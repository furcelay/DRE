import multiprocessing as mp
from queue import Full as QueueFull
from queue import Empty as QueueEmpty
from threading import Thread
import ctypes
import numpy as np
import numpy
from scipy.signal import fftconvolve
from functools import partial
from h5py import File
from DRE.core.models import ModelsCube
from DRE.core.results import Summary
from DRE.misc.progress_bar import progress
from DRE.misc.read_psf import get_psf
from astropy.io import fits
import os
import time
import datetime


class ModelCPU(ModelsCube):
    def __init__(self, models_file=None, out_compression='none', save_mosaics=False):
        super().__init__(models_file, out_compression)

        # send all attributes to shared memory, lock is not needed as is read-only
        self.models = self.to_shared_mem(self.models)
        self.log_r = self.to_shared_mem(self.log_r)
        self.ax_ratio = self.to_shared_mem(self.ax_ratio)
        self.angle = self.to_shared_mem(self.angle)
        self.save_mosaics = mp.Value(ctypes.c_bool, save_mosaics, lock=False)

    @staticmethod
    def to_shared_mem(array):
        shape = array.shape
        shared_array_base = mp.Array(ctypes.c_float, int(np.prod(shape)), lock=False)
        shared_array = np.ctypeslib.as_array(shared_array_base)
        shared_array = shared_array.reshape(shape)
        shared_array[:] = array
        return shared_array

    def convolve(self, psf_file, n_proc=1, *args, **kwargs):
        psf = get_psf(psf_file)
        convolve = partial(fftconvolve, in2=psf, mode='same', axes=(-2, -1))
        # (10, 13, 21, 128, 128) -> (10 * 13 * 21, 128, 128)
        flatten_shape = (np.prod(self.models.shape[:-2]), *self.models.shape[-2:])
        with mp.Pool(n_proc) as pool:
            # convolve in parallel
            convolved = pool.map(convolve, self.models.reshape(flatten_shape))
        convolved = np.array(list(convolved)).reshape(self.models.shape)
        self.convolved_models = self.to_shared_mem(convolved)


class Parallelize:

    def __init__(self, n_proc, max_size=100):
        self.n_proc = n_proc
        self.processes = []
        self.input_queue = mp.JoinableQueue(max_size)
        self.output_queue = mp.JoinableQueue()
        self.feed_thread = None
        self.output_thread = None
        self.terminate = mp.Event()

    @staticmethod
    def cpu_worker(model, input_queue, output_queue, terminate):
        try:
            for name, data, segment, noise in iter(input_queue.get, 'STOP'):
                chi_cube = model.dre_fit(data, segment, noise, backend=numpy)
                parameters = None
                mosaic = None
                success = not np.isnan(chi_cube).all()
                if success:
                    parameters = model.get_parameters(chi_cube)
                    ext, numb = name.split('_')
                    parameters['EXT_NUMBER'] = int(ext)
                    parameters['NUMBER'] = int(numb)
                    if model.save_mosaics:
                        model_idx = (parameters['E_IDX'], parameters['T_IDX'], parameters['R_IDX'])
                        mosaic = model.make_mosaic(data, segment, model_idx)
                output_queue.put((name, success, chi_cube, parameters, mosaic))
                input_queue.task_done()
        except KeyboardInterrupt:
            terminate.set()

    def feed_workers(self, names, input_file):
        submitted = 0
        while submitted < len(names) and not self.terminate.is_set():
            with File(input_file, 'r') as input_h5f:
                name = names[submitted]
                data = input_h5f[name]
                # subscribes input to workers queue
                try:
                    self.input_queue.put((name, data['obj'][:], data['seg'][:], data['rms'][:]), timeout=2)
                    submitted += 1
                except QueueFull:
                    pass
        if not self.terminate.is_set():
            for i in range(self.n_proc):
                self.input_queue.put('STOP')

    def get_output(self, model, input_name, n_tasks, output_file, table, progress_status):
        completed = 0
        while completed < n_tasks and not self.terminate.is_set():
            try:
                name, success, chi_cube, params, mosaic = self.output_queue.get(timeout=2)
                if success:
                    table.append(params)
                    with File(output_file, 'a') as output_h5f:
                        output_h5f.create_dataset(f'{name}', data=chi_cube,
                                                  dtype='float32', **model.compression)
                    if model.save_mosaics:
                        os.makedirs(os.path.join("Mosaics", input_name), exist_ok=True)
                        mosaic_fits = fits.ImageHDU(data=mosaic)
                        mosaic_fits.writeto(os.path.join("Mosaics", input_name, f"{input_name}_{name}_mosaic.fits"),
                                            overwrite=True)
                self.output_queue.task_done()
                completed += 1
                progress(completed, n_tasks, progress_status)
            except QueueEmpty:
                pass

    def start_processes(self, model, input_name, names, input_file, output_file, table, progress_status):
        self.feed_thread = Thread(target=self.feed_workers, args=(names, input_file))
        self.output_thread = Thread(target=self.get_output, args=(model, input_name, len(names), output_file,
                                                                  table, progress_status))
        self.feed_thread.start()
        self.output_thread.start()
        self.processes = []
        for i in range(self.n_proc):
            p = mp.Process(target=self.cpu_worker, args=(model, self.input_queue, self.output_queue, self.terminate))
            self.processes.append(p)
            p.start()

    def stop_processes(self):
        if self.feed_thread.is_alive():
            self.feed_thread.join()
        for p in self.processes:
            if p.is_alive():
                p.join()
        self.output_thread.join()

    def abort(self):
        self.terminate.set()
        self.input_queue.cancel_join_thread()
        self.output_queue.cancel_join_thread()

    def fit_file(self, model, input_name, input_file, output_file, psf, cats_dir, progress_status=''):
        with File(input_file, 'r') as input_h5f:
            names = list(input_h5f.keys())
        print(f"{progress_status}: {input_file}\t{len(names)} objects")
        # table with summary
        table = Summary(input_name)
        # convolve with the psf
        print(f"{progress_status}: Convolving...")
        convolve_start = time.time()
        try:
            model.convolve(psf, n_proc=self.n_proc)
        except FileNotFoundError:
            print(f"{progress_status}: Warning: Cannot find the PSF file {psf},\n\t skipping this tile")
            return
        print(f"{progress_status}: Convolved! ({time.time() - convolve_start:2.2f}s)")
        # fit in parallel
        job_start = time.time()
        try:
            self.start_processes(model, input_name, names, input_file, output_file, table, progress_status)
            self.stop_processes()
            # save summary
            table.save(cats_dir)
            time_delta = datetime.timedelta(seconds=(time.time() - job_start))
            obj_s = len(names) / (time.time() - job_start)
            print(f"\n{progress_status}: finished in {str(time_delta)[:10]}s ({obj_s:1.3f} obj/s)")
        except KeyboardInterrupt:
            print("\nAborted by user request")
            self.abort()

    def fit_dir(self, model, input_dir='Cuts', output_dir='Chi', psf_dir='PSF', cats_dir='Sextracted'):
        print("Running DRE")
        start = time.time()
        # list with input files in input_dir
        files = os.listdir(input_dir)
        os.makedirs(output_dir, exist_ok=True)
        for i, filename in enumerate(sorted(files)):
            input_file = os.path.join(input_dir, filename)
            name = os.path.basename(filename).replace('_cuts.h5', '')
            output_file = os.path.join(output_dir, f"{name}_chi.h5")
            psf = os.path.join(psf_dir, f"{name}.psf")
            if os.path.isfile(output_file):
                os.remove(output_file)
            # fit all cuts in each file
            self.fit_file(model, name, input_file, output_file, psf, cats_dir,
                          progress_status=f"({i + 1}/{len(files)})")
            if self.terminate.is_set():
                break
        time_delta = datetime.timedelta(seconds=(time.time() - start))
        print(f"DRE finished, total time: {str(time_delta)[:10]}s")
