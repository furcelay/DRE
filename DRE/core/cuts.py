from astropy.io import fits
from astropy.nddata import Cutout2D
from scipy.ndimage import shift, binary_dilation, measurements, binary_fill_holes
from photutils.centroids import centroid_com, centroid_1dg, centroid_2dg
from h5py import File
import numpy as np
import os
from DRE.misc.progress_bar import progress
from DRE.misc.h5py_compression import compression_types
from DRE.misc.read_catalog import cat_to_table


class Cutter:

    def __init__(self, margin=80, max_stellarity=1.0, filters=None, centroids=False, centroids_mode='com',
                 compression='none', image_size=128):

        self.margin = margin
        self.max_stellarity = max_stellarity
        self.image_size = image_size
        self.centroids = centroids
        self.centroid_func = {'com': centroid_com, '1dg': centroid_1dg, '2dg': centroid_2dg}[centroids_mode]

        self.compression = compression_types[compression]
        if filters is None:
            self.extra_filters = []
        else:
            self.extra_filters = filters

    def condition(self, _row, header):
        conditions = []
        inside_x = self.margin < _row['X_IMAGE'] < header['NAXIS1'] - self.margin
        inside_y = self.margin < _row["Y_IMAGE"] < header['NAXIS2'] - self.margin
        if self.max_stellarity >= 1:
            is_galaxy = True
        else:
            is_galaxy = _row['CLASS_STAR'] < self.max_stellarity
        conditions.extend([inside_x, inside_y, is_galaxy])
        for param, _min, _max in self.extra_filters:
            new_condition = float(_min) < _row[param] < float(_max)
            conditions.append(new_condition)
        if all(conditions):
            return True
        else:
            return False

    def cut_object(self, fits_data, cat_row, ext_number):
        return Cutout2D(fits_data[ext_number].data,
                        (cat_row["X_IMAGE"] - 1, cat_row["Y_IMAGE"] - 1),
                        self.image_size).data.copy()

    @staticmethod
    def clean_mask(mask, min_size=4, dilation=4):
        # add a minimal mask at the center
        mask[mask.shape[0] // 2 - min_size // 2:mask.shape[0] // 2 + min_size // 2,
             mask.shape[1] // 2 - min_size // 2:mask.shape[1] // 2 + min_size // 2] = 1
        # get the central cluster
        clusters, _ = measurements.label(mask)
        central_cluster = clusters[mask.shape[0] // 2, mask.shape[1] // 2]
        mask = (clusters == central_cluster).astype(int)
        # fill holes
        mask = binary_fill_holes(mask)
        # dilation
        mask = binary_dilation(mask, iterations=dilation)
        return mask

    def cut_image(self, cat, out_name, seg, obj, noise, progress_status):
        cut = 0
        with File(out_name, 'w') as h5_file:
            for j, row in enumerate(cat):
                ext_number = row['EXT_NUMBER'] if 'EXT_NUMBER' in row.keys() else 0
                if self.condition(row, obj[ext_number].header):
                    # filters the data that contains nan's

                    # cuts
                    obj_cut = self.cut_object(obj, row, ext_number)
                    seg_cut = self.cut_object(seg, row, ext_number)
                    rms_cut = self.cut_object(noise, row, ext_number)

                    # SExtractor hides nan's as -1e30, skip cuts with nan's
                    if np.isclose(obj_cut, -1e30).any():
                        continue

                    # mask
                    seg_cut = self.clean_mask(np.equal(seg_cut, row["NUMBER"]))

                    # centroid + shift
                    if self.centroids:
                        x_shift, y_shift = (self.image_size - 1)/2 - self.centroid_func(obj_cut, mask=~seg_cut)
                        obj_cut = shift(obj_cut, (y_shift, x_shift))
                        # order 0 for segment as is binary
                        seg_cut = shift(seg_cut, (y_shift, x_shift), order=0)
                        rms_cut = shift(rms_cut, (y_shift, x_shift))

                    h5_group = h5_file.create_group(f"{ext_number:02d}_{row['NUMBER']:04d}")
                    h5_group.create_dataset('obj', data=obj_cut,
                                            dtype='float32', **self.compression)
                    h5_group.create_dataset('seg', data=seg_cut,
                                            dtype='bool', **self.compression)
                    h5_group.create_dataset('rms', data=rms_cut,
                                            dtype='float32', **self.compression)

                    progress(j + 1, len(cat), progress_status)
                    cut += 1
        print(f"\n{progress_status}: {cut} cuts")

    def cut_tiles(self, tiles='Tiles', sextracted='Sextracted', catalogs=None, output='Cuts'):
        # walk directory recursively
        _, _, files = next(os.walk(tiles))
        os.makedirs(output, exist_ok=True)
        for i, filename in enumerate(sorted(files)):
            name, _ = os.path.splitext(os.path.split(filename)[1])
            if os.path.isdir(os.path.join(sextracted, name)):
                basename = os.path.join(sextracted, name, name)
            else:
                basename = os.path.join(sextracted, name)

            seg = fits.open(f"{basename}_seg.fits")
            obj = fits.open(f"{basename}_nb.fits")
            noise = fits.open(f"{basename}_rms.fits")
            if catalogs is None:
                cat = cat_to_table(f"{basename}_cat.fits")
            else:
                cat = cat_to_table(os.path.join(catalogs, f"{name}_cat.fits"))

            out_name = os.path.join(output, f"{name}_cuts.h5")
            if os.path.isfile(out_name):
                os.remove(out_name)
            progress_status = f"({i + 1}/{len(files)})"
            print(f"{progress_status}: {name}")
            self.cut_image(cat, out_name,
                           seg, obj, noise,
                           progress_status)
            seg.close()
            obj.close()
            noise.close()
