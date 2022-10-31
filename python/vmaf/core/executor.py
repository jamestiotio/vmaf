from abc import ABCMeta, abstractmethod
import multiprocessing
import os
from time import sleep
import hashlib

import numpy as np

from vmaf.core.asset import Asset
from vmaf.tools.decorator import deprecated, override

from vmaf.tools.misc import make_parent_dirs_if_nonexist, get_dir_without_last_slash, \
    parallel_map, match_any_files, run_process, \
    get_file_name_extension, get_normalized_string_from_dict
from vmaf.core.mixin import TypeVersionEnabled
from vmaf.config import VmafExternalConfig
from vmaf.tools.reader import YuvReader
from vmaf.tools.writer import YuvWriter

__copyright__ = "Copyright 2016-2020, Netflix, Inc."
__license__ = "BSD+Patent"


class Executor(TypeVersionEnabled):
    """
    An Executor takes in a list of Assets, and run computations on them, and
    return a list of corresponding Results. An Executor must specify a unique
    type and version combination (by the TYPE and VERSION attribute), so that
    the Result generated by it can be uniquely identified.

    Executor is the base class for FeatureExtractor and QualityRunner, and it
    provides a number of shared housekeeping functions, including reusing
    Results, creating FIFO pipes, cleaning up log files/Results, etc.

    (3/11/2020) added an (optional) step to allow python-based processing on
    both the ref and dis files. The new processing pipeline looks like this:

     notyuv  --------   ref_workfile   -----------------   ref_procfile
    -------> |FFmpeg| ---------------> |python-callback| -----------------
             --------                  -----------------                 |    -----------------
                                                                         ---> |               |
                                                                              |     VMAF      | --->
                                                                         ---> |               |
     notyuv  --------   dis_workfile   -----------------   dis_procfile  |    -----------------
    -------> |FFmpeg| ---------------> |python-callback| ----------------
             --------                  -----------------
    """

    __metaclass__ = ABCMeta

    @abstractmethod
    def _generate_result(self, asset):
        raise NotImplementedError

    @abstractmethod
    def _read_result(self, asset):
        raise NotImplementedError

    def __init__(self,
                 assets,
                 logger,
                 fifo_mode=True,
                 delete_workdir=True,
                 result_store=None,
                 optional_dict=None,
                 optional_dict2=None,
                 save_workfiles=False,
                 ):
        """
        Use optional_dict for parameters that would impact result (e.g. model,
        patch size), and use optional_dict2 for parameters that would NOT
        impact result (e.g. path to data cache file).
        """

        TypeVersionEnabled.__init__(self)

        self.assets = assets
        self.logger = logger
        self.fifo_mode = fifo_mode
        self.delete_workdir = delete_workdir
        self.results = []
        self.result_store = result_store
        self.optional_dict = optional_dict
        self.optional_dict2 = optional_dict2
        self.save_workfiles = save_workfiles

        self._assert_class()
        self._assert_args()
        self._assert_assets()

        self._custom_init()

    def _custom_init(self):
        pass

    @staticmethod
    def get_normalized_string_from_dict(d):
        """ Normalized string representation with sorted keys, extended to cover callables.

        >>> Executor.get_normalized_string_from_dict({"max_buffer_sec": 5.0, "bitrate_kbps": 45, })
        'bitrate_kbps_45_max_buffer_sec_5.0'
        >>> Executor.get_normalized_string_from_dict({"sorted_key": sorted, "_need_ffmpeg_key": Executor._need_ffmpeg, })
        '_need_ffmpeg_key_Executor._need_ffmpeg_sorted_key_sorted'
        """
        def _slugify(v):
            if callable(v):
                s = str(v)
                assert s[0] == '<' and s[-1] == '>'
                s = s[1:-1]
                l = s.split(' ')
                assert 'function' in l
                for idx, e in enumerate(l):
                    if e == 'function':
                        assert idx < len(l) - 1
                        return l[idx + 1]
            else:
                return v
        return '_'.join(map(lambda k: '{k}_{v}'.format(k=k,v=_slugify(d[k])), sorted(d.keys())))

    @property
    def executor_id(self):
        executor_id_ = TypeVersionEnabled.get_type_version_string(self)

        if self.optional_dict is not None and len(self.optional_dict) > 0:
            # include optional_dict info in executor_id for result store,
            # as parameters in optional_dict will impact result
            executor_id_ += '_{}'.format(self.get_normalized_string_from_dict(self.optional_dict))
            replace_chars = ["'", " "]
            for c in replace_chars:
                executor_id_ = executor_id_.replace(c, "_")
        return executor_id_

    def run(self, **kwargs):
        """
        Do all the computation here.
        :return:
        """
        if self.logger:
            self.logger.info(
                "For each asset, if {type} result has not been generated, run "
                "and generate {type} result...".format(type=self.executor_id))

        if 'parallelize' in kwargs:
            parallelize = kwargs['parallelize']
        else:
            parallelize = False
        assert isinstance(parallelize, bool)

        if 'processes' in kwargs and kwargs['processes'] is not None:
            assert parallelize is True, 'Cannot specify processes if parallelize is False.'
            processes = kwargs['processes']
        else:
            processes = None
        assert processes is None or (isinstance(processes, int) and processes >= 1)

        if parallelize:
            # create locks for unique assets (uniqueness is identified by str(asset))
            map_asset_lock = {}
            locks = []
            for asset in self.assets:
                asset_str = str(asset)
                if asset_str not in map_asset_lock:
                    map_asset_lock[asset_str] = multiprocessing.Lock()
                locks.append(map_asset_lock[asset_str])

            # pack key arguments to be used as inputs to map function
            list_args = []
            for asset, lock in zip(self.assets, locks):
                list_args.append(
                    [asset, lock])

            def _run(asset_lock):
                asset, lock = asset_lock
                lock.acquire()
                result = self._run_on_asset(asset)
                lock.release()
                return result

            self.results = parallel_map(_run, list_args, processes=processes)
        else:
            self.results = list(map(self._run_on_asset, self.assets))

    def remove_results(self):
        """
        Remove all relevant Results stored in ResultStore, which is specified
        at the constructor.
        :return:
        """
        for asset in self.assets:
            self._remove_result(asset)

    @classmethod
    def _assert_class(cls):
        pass

    def _assert_args(self):
        if self.save_workfiles is True:
            assert self.fifo_mode is False, 'To save workfiles, FIFO mode cannot be true.'

    def _assert_assets(self):

        for asset in self.assets:
            self._assert_an_asset(asset)

        pass

    @staticmethod
    def _need_ffmpeg(asset):
        # 1) if quality width/height do not agree with ref/dis width/height,
        # must rely on ffmpeg for scaling
        # 2) if crop/pad/etc. is needed, need ffmpeg
        # 3) if ref/dis videos' start/end frames specified, need ffmpeg for
        # frame extraction
        # 4) if workfile_yuv_type specified and ref/yuv's yuv_type does not agree,
        # need ffmpeg for conversion
        ret = asset.quality_width_height != asset.ref_width_height \
               or asset.quality_width_height != asset.dis_width_height \
               or asset.ref_yuv_type == 'notyuv' or asset.dis_yuv_type == 'notyuv' \
               or asset.ref_start_end_frame is not None or asset.dis_start_end_frame is not None \
               or 'workfile_yuv_type' in asset.asset_dict and (asset.workfile_yuv_type != asset.ref_yuv_type or asset.workfile_yuv_type != asset.dis_yuv_type)
        for key in Asset.ORDERED_FILTER_LIST:
            ret = ret or asset.get_filter_cmd(key, 'ref') is not None or asset.get_filter_cmd(key, 'dis') is not None

        return ret

    @classmethod
    def _assert_an_asset(cls, asset):

        # needed by _generate_result, and by _open_ref_workfile or
        # _open_dis_workfile if called
        assert asset.quality_width_height is not None

        if cls._need_ffmpeg(asset):
            VmafExternalConfig.get_and_assert_ffmpeg()

        # ref_yuv_type and dis_yuv_type must match, unless any of them is notyuv.
        # also check the logic in _get_workfile_yuv_type
        assert (asset.ref_yuv_type == 'notyuv' or asset.dis_yuv_type == 'notyuv') \
               or (asset.ref_yuv_type == asset.dis_yuv_type) or "workfile_yuv_type" in asset.asset_dict

        # if crop_cmd or pad_cmd or etc. is specified, make sure quality_width and
        # quality_height are EXPLICITLY specified in asset_dict
        if asset.ref_crop_cmd is not None or asset.dis_crop_cmd is not None:
            assert 'quality_width' in asset.asset_dict and 'quality_height' in asset.asset_dict, \
                'If crop_cmd or etc. is specified, must also EXPLICITLY specify quality_width and quality_height.'
        if asset.ref_pad_cmd is not None or asset.dis_pad_cmd is not None:
            assert 'quality_width' in asset.asset_dict and 'quality_height' in asset.asset_dict, \
                'If pad_cmd or etc. is specified, must also EXPLICITLY specify quality_width and quality_height.'

    @staticmethod
    def _get_workfile_yuv_type(asset):
        """ Same as original yuv type, unless it is notyuv; in this case, check
        the other's (if ref, check dis'; vice versa); if both notyuv, use format as set at a higher level"""

        # also check the logic in _assert_an_asset. The assumption is:
        # assert (asset.ref_yuv_type == 'notyuv' or asset.dis_yuv_type == 'notyuv') \
        #        or (asset.ref_yuv_type == asset.dis_yuv_type)

        # if the workfile_yuv_type is provided by the user, then we use it
        if "workfile_yuv_type" in asset.asset_dict:
            return asset.workfile_yuv_type

        if asset.ref_yuv_type == 'notyuv' and asset.dis_yuv_type == 'notyuv':
            return asset.workfile_yuv_type
        elif asset.ref_yuv_type == 'notyuv' and asset.dis_yuv_type != 'notyuv':
            return asset.dis_yuv_type
        elif asset.ref_yuv_type != 'notyuv' and asset.dis_yuv_type == 'notyuv':
            return asset.ref_yuv_type
        else: # neither notyuv
            assert asset.ref_yuv_type == asset.dis_yuv_type, "YUV types for ref and dis do not match."
            return asset.ref_yuv_type

    def _wait_for_workfiles(self, asset):
        # wait til workfile paths being generated
        for i in range(10):
            if os.path.exists(asset.ref_workfile_path) and os.path.exists(asset.dis_workfile_path):
                break
            sleep(0.1)
        else:
            raise RuntimeError("ref or dis video workfile path {ref} or {dis} is missing.".format(ref=asset.ref_workfile_path, dis=asset.dis_workfile_path))

    def _wait_for_procfiles(self, asset):
        # wait til procfile paths being generated
        for i in range(10):
            if os.path.exists(asset.ref_procfile_path) and os.path.exists(asset.dis_procfile_path):
                break
            sleep(0.1)
        else:
            raise RuntimeError("ref or dis video procfile path {ref} or {dis} is missing.".format(ref=asset.ref_procfile_path, dis=asset.dis_procfile_path))

    def _prepare_log_file(self, asset):

        log_file_path = self._get_log_file_path(asset)

        # if parent dir doesn't exist, create
        make_parent_dirs_if_nonexist(log_file_path)

        # add runner type and version
        with open(log_file_path, 'wt') as log_file:
            log_file.write("{type_version_str}\n\n".format(
                type_version_str=self.get_cozy_type_version_string()))

    def _assert_paths(self, asset):
        assert os.path.exists(asset.ref_path) or match_any_files(asset.ref_path), \
            "Reference path {} does not exist.".format(asset.ref_path)
        assert os.path.exists(asset.dis_path) or match_any_files(asset.dis_path), \
            "Distorted path {} does not exist.".format(asset.dis_path)

    def _run_on_asset(self, asset):
        # Wraper around the essential function _generate_result, to
        # do housekeeping work including 1) asserts of asset, 2) skip run if
        # log already exist, 3) creating fifo, 4) delete work file and dir

        if self.result_store:
            result = self.result_store.load(asset, self.executor_id)
        else:
            result = None

        # if result can be retrieved from result_store, skip log file
        # generation and reading result from log file, but directly return
        # return the retrieved result
        if result is not None:
            if self.logger:
                self.logger.info('{id} result exists. Skip {id} run.'.
                                 format(id=self.executor_id))
        else:

            if self.logger:
                self.logger.info('{id} result does\'t exist. Perform {id} '
                                 'calculation.'.format(id=self.executor_id))

            # at this stage, it is certain that asset.ref_path and
            # asset.dis_path will be used. must early determine that
            # they exists
            self._assert_paths(asset)

            # if no FFmpeg is involved, directly work on ref_path/dis_path,
            # instead of opening workfiles
            self._set_asset_use_path_as_workpath(asset)

            # if no ref/dis_proc_callback is involved, directly work on ref/dis_workfile_path,
            # instead of opening procfiles
            self._set_asset_use_workpath_as_procpath(asset)

            # remove workfiles if exist (do early here to avoid race condition
            # when ref path and dis path have some overlap)
            if asset.use_path_as_workpath:
                # do nothing
                pass
            else:
                self._close_workfiles(asset)

            # remove procfiles if exist (do early here to avoid race condition
            # when ref path and dis path have some overlap)
            if asset.use_workpath_as_procpath:
                # do nothing
                pass
            else:
                self._close_procfiles(asset)

            log_file_path = self._get_log_file_path(asset)
            make_parent_dirs_if_nonexist(log_file_path)

            if asset.use_path_as_workpath:
                # do nothing
                pass
            else:
                if self.fifo_mode:
                    self._open_workfiles_in_fifo_mode(asset)
                else:
                    self._open_workfiles(asset)

            if asset.use_workpath_as_procpath:
                # do nothing
                pass
            else:
                if self.fifo_mode:
                    self._open_procfiles_in_fifo_mode(asset)
                else:
                    self._open_procfiles(asset)

            self._prepare_log_file(asset)

            self._generate_result(asset)

            if self.logger:
                self.logger.info("Read {id} log file, get scores...".
                                 format(id=self.executor_id))

            # collect result from each asset's log file
            result = self._read_result(asset)

            # save result
            if self.result_store:
                result = self._save_result(result)

            # clean up workfiles
            if self.delete_workdir:
                if asset.use_path_as_workpath:
                    # do nothing
                    pass
                else:
                    self._close_workfiles(asset)

                if asset.use_workpath_as_procpath:
                    # do nothing
                    pass
                else:
                    self._close_procfiles(asset)

            # clean up workdir and log files in it
            if self.delete_workdir:

                # remove log file
                self._remove_log(asset)

                # remove dir
                log_file_path = self._get_log_file_path(asset)
                log_dir = get_dir_without_last_slash(log_file_path)
                try:
                    os.rmdir(log_dir)
                except OSError as e:
                    if e.errno == 39: # [Errno 39] Directory not empty
                        # e.g. VQM could generate an error file with non-critical
                        # information like: '3 File is longer than 15 seconds.
                        # Results will be calculated using first 15 seconds
                        # only.' In this case, want to keep this
                        # informational file and pass
                        pass

        result = self._post_process_result(result)

        return result

    def _open_workfiles(self, asset):
        self._open_ref_workfile(asset, fifo_mode=False)
        self._open_dis_workfile(asset, fifo_mode=False)

    def _open_workfiles_in_fifo_mode(self, asset):
        ref_p = multiprocessing.Process(target=self._open_ref_workfile,
                                        args=(asset, True))
        dis_p = multiprocessing.Process(target=self._open_dis_workfile,
                                        args=(asset, True))
        ref_p.start()
        dis_p.start()
        self._wait_for_workfiles(asset)

    def _open_procfiles(self, asset):
        self._open_ref_procfile(asset, fifo_mode=False)
        self._open_dis_procfile(asset, fifo_mode=False)

    def _open_procfiles_in_fifo_mode(self, asset):
        ref_p = multiprocessing.Process(target=self._open_ref_procfile,
                                        args=(asset, True))
        dis_p = multiprocessing.Process(target=self._open_dis_procfile,
                                        args=(asset, True))
        ref_p.start()
        dis_p.start()
        self._wait_for_procfiles(asset)

    def _close_workfiles(self, asset):
        self._close_ref_workfile(asset)
        self._close_dis_workfile(asset)

    @classmethod
    def _close_procfiles(cls, asset):
        cls._close_ref_procfile(asset)
        cls._close_dis_procfile(asset)

    def _refresh_workfiles_before_additional_pass(self, asset):
        # If fifo mode and workpath needs to be freshly generated, must
        # reopen the fifo pipe before proceeding
        if self.fifo_mode and (not asset.use_path_as_workpath):
            self._close_workfiles(asset)
            self._open_workfiles_in_fifo_mode(asset)

    def _save_result(self, result):
        self.result_store.save(result)
        if self.save_workfiles:
            self.result_store.save_workfile(result, result.asset.ref_workfile_path, '_ref')
            self.result_store.save_workfile(result, result.asset.dis_workfile_path, '_dis')
        return result

    @classmethod
    def _set_asset_use_path_as_workpath(cls, asset):
        # if no rescaling or croping or padding or etc. is involved, directly work on
        # ref_path/dis_path, instead of opening workfiles
        if not cls._need_ffmpeg(asset):
            asset.use_path_as_workpath = True

    @classmethod
    def _set_asset_use_workpath_as_procpath(cls, asset):
        if asset.ref_proc_callback is None and asset.dis_proc_callback is None:
            asset.use_workpath_as_procpath = True

    @classmethod
    def _post_process_result(cls, result):
        # do nothing, wait to be overridden
        return result

    def _get_log_file_path(self, asset):
        return "{workdir}/{executor_id}_{str}".format(
            workdir=asset.workdir, executor_id=self.executor_id,
            str=hashlib.sha1(str(asset).encode("utf-8")).hexdigest())

    # ===== workfile =====

    def _open_ref_workfile(self, asset, fifo_mode):

        use_path_as_workpath = asset.use_path_as_workpath
        path = asset.ref_path
        workfile_path = asset.ref_workfile_path
        quality_width_height = self._get_quality_width_height(asset)
        yuv_type = asset.ref_yuv_type
        resampling_type = self._get_ref_resampling_type(asset)
        width_height = asset.ref_width_height
        ref_or_dis = 'ref'
        workfile_yuv_type = self._get_workfile_yuv_type(asset)
        logger = self.logger

        _open_workfile_method = self.optional_dict['_open_workfile_method'] \
            if self.optional_dict is not None and '_open_workfile_method' in self.optional_dict and self.optional_dict['_open_workfile_method'] is not None \
            else self._open_workfile
        _open_workfile_method(self, asset, path, workfile_path, yuv_type, workfile_yuv_type, resampling_type, width_height,
                              quality_width_height, ref_or_dis, use_path_as_workpath, fifo_mode, logger)

    def _open_dis_workfile(self, asset, fifo_mode):

        use_path_as_workpath = asset.use_path_as_workpath
        path = asset.dis_path
        workfile_path = asset.dis_workfile_path
        quality_width_height = self._get_quality_width_height(asset)
        yuv_type = asset.dis_yuv_type
        resampling_type = self._get_dis_resampling_type(asset)
        width_height = asset.dis_width_height
        ref_or_dis = 'dis'
        workfile_yuv_type = self._get_workfile_yuv_type(asset)
        logger = self.logger

        _open_workfile_method = self.optional_dict['_open_workfile_method'] \
            if self.optional_dict is not None and '_open_workfile_method' in self.optional_dict and self.optional_dict['_open_workfile_method'] is not None \
            else self._open_workfile
        _open_workfile_method(self, asset, path, workfile_path, yuv_type, workfile_yuv_type, resampling_type, width_height,
                              quality_width_height, ref_or_dis, use_path_as_workpath, fifo_mode, logger)

    @staticmethod
    def _open_workfile(cls, asset, path, workfile_path, yuv_type, workfile_yuv_type, resampling_type, width_height,
                       quality_width_height, ref_or_dis, use_path_as_workpath, fifo_mode, logger):
        # only need to open workfile if the path is different from path
        assert use_path_as_workpath is False and path != workfile_path
        # if fifo mode, mkfifo
        if fifo_mode:
            os.mkfifo(workfile_path)
        if yuv_type != 'notyuv':
            # in this case, for sure has width_height
            assert width_height is not None
            width, height = width_height
            src_fmt_cmd = cls._get_yuv_src_fmt_cmd(asset, height, width, ref_or_dis)
        else:
            src_fmt_cmd = cls._get_notyuv_src_fmt_cmd(asset, ref_or_dis)
        vframes_cmd, select_cmd = cls._get_vframes_cmd(asset, ref_or_dis)
        crop_cmd = cls._get_filter_cmd(asset, 'crop', ref_or_dis)
        pad_cmd = cls._get_filter_cmd(asset, 'pad', ref_or_dis)
        quality_width, quality_height = quality_width_height
        scale_cmd = 'scale={width}x{height}'.format(width=quality_width, height=quality_height)
        filter_cmds = []
        for key in Asset.ORDERED_FILTER_LIST:
            if key != 'crop' and key != 'pad':
                filter_cmds.append(cls._get_filter_cmd(asset, key, ref_or_dis))
        vf_cmd = ','.join(filter(lambda s: s != '', [select_cmd, crop_cmd, pad_cmd, scale_cmd] + filter_cmds))
        ffmpeg_cmd = '{ffmpeg} {src_fmt_cmd} -i {src} -an -vsync 0 ' \
                     '-pix_fmt {yuv_type} {vframes_cmd} -vf {vf_cmd} -f rawvideo ' \
                     '-sws_flags {resampling_type} -y -nostdin {dst}'.format(
            ffmpeg=VmafExternalConfig.get_and_assert_ffmpeg(),
            src=path,
            dst=workfile_path,
            src_fmt_cmd=src_fmt_cmd,
            vf_cmd=vf_cmd,
            yuv_type=workfile_yuv_type,
            resampling_type=resampling_type,
            vframes_cmd=vframes_cmd,
        )
        if logger:
            logger.info(ffmpeg_cmd)
        run_process(ffmpeg_cmd, shell=True, env=VmafExternalConfig.ffmpeg_env())

    # ===== procfile =====

    def _open_ref_procfile(self, asset, fifo_mode):

        # only need to open ref procfile if the path is different from ref path
        assert asset.use_workpath_as_procpath is False and asset.ref_workfile_path != asset.ref_procfile_path

        ref_proc_callback = asset.ref_proc_callback if asset.ref_proc_callback is not None else lambda x: x

        if fifo_mode:
            os.mkfifo(asset.ref_procfile_path)

        quality_width, quality_height = self._get_quality_width_height(asset)
        yuv_type = asset.workfile_yuv_type
        with YuvReader(filepath=asset.ref_workfile_path, width=quality_width, height=quality_height,
                       yuv_type=yuv_type) as ref_yuv_reader:
            with YuvWriter(filepath=asset.ref_procfile_path, width=quality_width, height=quality_height,
                           yuv_type=yuv_type) as ref_yuv_writer:
                while True:
                    try:
                        y, u, v = ref_yuv_reader.next(format='float')
                        y, u, v = ref_proc_callback(y), u, v
                        ref_yuv_writer.next(y, u, v, format='float2uint')
                    except StopIteration:
                        break

    def _open_dis_procfile(self, asset, fifo_mode):

        # only need to open dis procfile if the path is different from dis path
        assert asset.use_workpath_as_procpath is False and asset.dis_workfile_path != asset.dis_procfile_path

        dis_proc_callback = asset.dis_proc_callback if asset.dis_proc_callback is not None else lambda x: x

        if fifo_mode:
            os.mkfifo(asset.dis_procfile_path)

        quality_width, quality_height = self._get_quality_width_height(asset)
        yuv_type = asset.workfile_yuv_type
        with YuvReader(filepath=asset.dis_workfile_path, width=quality_width, height=quality_height,
                       yuv_type=yuv_type) as dis_yuv_reader:
            with YuvWriter(filepath=asset.dis_procfile_path, width=quality_width, height=quality_height,
                           yuv_type=yuv_type) as dis_yuv_writer:
                while True:
                    try:
                        y, u, v = dis_yuv_reader.next(format='float')
                        y, u, v = dis_proc_callback(y), u, v
                        dis_yuv_writer.next(y, u, v, format='float2uint')
                    except StopIteration:
                        break

    def _get_ref_resampling_type(self, asset):
        return asset.ref_resampling_type

    def _get_dis_resampling_type(self, asset):
        return asset.dis_resampling_type

    def _get_quality_width_height(self, asset):
        return asset.quality_width_height

    @staticmethod
    def _get_yuv_src_fmt_cmd(asset, height, width, ref_or_dis):
        if ref_or_dis == 'ref':
            yuv_type = asset.ref_yuv_type
        elif ref_or_dis == 'dis':
            yuv_type = asset.dis_yuv_type
        else:
            raise AssertionError('Unknown ref_or_dis: {}'.format(ref_or_dis))
        yuv_src_fmt_cmd = '-f rawvideo -pix_fmt {yuv_fmt} -s {width}x{height}'. \
            format(yuv_fmt=yuv_type, width=width, height=height)
        return yuv_src_fmt_cmd

    @staticmethod
    def _get_notyuv_src_fmt_cmd(asset, target):
        if target == 'ref':
            path = asset.ref_path
        elif target == 'dis':
            path = asset.dis_path
        else:
            assert False, 'target cannot be {}'.format(target)

        if get_file_name_extension(path) in ['j2c', 'j2k', 'tiff']:
            # 2147483647 is INT_MAX if int is 4 bytes
            return "-f image2 -start_number_range 2147483647"
        elif get_file_name_extension(path) in ['icpf']:
            return "-f image2 -c:v netflixprores -start_number_range 2147483647"
        elif get_file_name_extension(path) in ['265']:
            return "-c:v hevc"
        else:
            return ""

    @staticmethod
    def _get_filter_cmd(asset, key, target):
        return "{}={}".format(key, asset.get_filter_cmd(key, target)) \
            if asset.get_filter_cmd(key, target) is not None else ""

    @staticmethod
    def _get_vframes_cmd(asset, ref_or_dis):
        if ref_or_dis == 'ref':
            start_end_frame = asset.ref_start_end_frame
        elif ref_or_dis == 'dis':
            start_end_frame = asset.dis_start_end_frame
        else:
            raise AssertionError('Unknown ref_or_dis: {}'.format(ref_or_dis))

        if start_end_frame is None:
            return "", ""
        else:
            start_frame, end_frame = start_end_frame
            num_frames = end_frame - start_frame + 1
            return f"-vframes {num_frames}", f"select='gte(n\\,{start_frame})*gte({end_frame}\\,n)',setpts=PTS-STARTPTS"

    def _close_ref_workfile(self, asset):

        use_path_as_workpath = asset.use_path_as_workpath
        path = asset.ref_path
        workfile_path = asset.ref_workfile_path

        _close_workfile_method = self.optional_dict['_close_workfile_method'] \
            if self.optional_dict is not None and '_close_workfile_method' in self.optional_dict and self.optional_dict['_close_workfile_method'] is not None \
            else self._close_workfile
        _close_workfile_method(path, workfile_path, use_path_as_workpath)

    def _close_dis_workfile(self, asset):

        use_path_as_workpath = asset.use_path_as_workpath
        path = asset.dis_path
        workfile_path = asset.dis_workfile_path

        _close_workfile_method = self.optional_dict['_close_workfile_method'] \
            if self.optional_dict is not None and '_close_workfile_method' in self.optional_dict and self.optional_dict['_close_workfile_method'] is not None \
            else self._close_workfile
        _close_workfile_method(path, workfile_path, use_path_as_workpath)

    @staticmethod
    def _close_workfile(path, workfile_path, use_path_as_workpath):

        # only need to close workfile if the workfile path is different from path
        assert use_path_as_workpath is False and path != workfile_path

        if os.path.exists(workfile_path):
            os.remove(workfile_path)

    @staticmethod
    def _close_ref_procfile(asset):

        # only need to close ref procfile if the path is different from ref workpath
        assert asset.use_workpath_as_procpath is False and asset.ref_workfile_path != asset.ref_procfile_path

        if os.path.exists(asset.ref_procfile_path):
            os.remove(asset.ref_procfile_path)

    @staticmethod
    def _close_dis_procfile(asset):

        # only need to close dis procfile if the path is different from dis path
        assert asset.use_workpath_as_procpath is False and asset.dis_workfile_path != asset.dis_procfile_path

        if os.path.exists(asset.dis_procfile_path):
            os.remove(asset.dis_procfile_path)

    def _remove_log(self, asset):
        log_file_path = self._get_log_file_path(asset)
        if os.path.exists(log_file_path):
            os.remove(log_file_path)

    def _remove_result(self, asset):
        if self.result_store:
            self.result_store.delete(asset, self.executor_id)
            self.result_store.delete_workfile(asset, self.executor_id, '_ref')
            self.result_store.delete_workfile(asset, self.executor_id, '_dis')

@deprecated
def run_executors_in_parallel(executor_class,
                              assets,
                              fifo_mode=True,
                              delete_workdir=True,
                              parallelize=True,
                              logger=None,
                              result_store=None,
                              optional_dict=None,
                              optional_dict2=None,
                              ):
    """
    Run multiple Executors in parallel.
    """

    # construct an executor object just to call _assert_assets() only
    executor_class(
        assets,
        logger,
        fifo_mode=fifo_mode,
        delete_workdir=True,
        result_store=result_store,
        optional_dict=optional_dict,
        optional_dict2=optional_dict2
    )

    # create locks for unique assets (uniqueness is identified by str(asset))
    map_asset_lock = {}
    locks = []
    for asset in assets:
        asset_str = str(asset)
        if asset_str not in map_asset_lock:
            map_asset_lock[asset_str] = multiprocessing.Lock()
        locks.append(map_asset_lock[asset_str])

    # pack key arguments to be used as inputs to map function
    list_args = []
    for asset, lock in zip(assets, locks):
        list_args.append(
            [executor_class, asset, fifo_mode, delete_workdir,
             result_store, optional_dict, optional_dict2, lock])

    def run_executor(args):
        executor_class, asset, fifo_mode, delete_workdir, \
        result_store, optional_dict, optional_dict2, lock = args
        lock.acquire()
        executor = executor_class([asset], None, fifo_mode, delete_workdir,
                                  result_store, optional_dict, optional_dict2)
        executor.run()
        lock.release()
        return executor

    # run
    if parallelize:
        executors = parallel_map(run_executor, list_args, processes=None)
    else:
        executors = list(map(run_executor, list_args))

    # aggregate results
    results = [executor.results[0] for executor in executors]

    return executors, results


class NorefExecutorMixin(object):
    """
    Override Executor whenever reference video is mentioned.

    NorefExecutorMixin is useful for NorefAsset, i.e. assets that does not have a reference.

    Example classes that inherits NorefExecutorMixin include: BrisqueNorefFeatureExtractor, VideoEncoder

    (3/11/2020) added an (optional) step to allow python-based processing on
    the dis file. The new processing pipeline looks like this:

     notyuv  --------   dis_workfile   -----------------    dis_procfile      -----------------
    -------> |FFmpeg| ---------------> |python-callback| -------------------> |   BRISQUE     | --->
             --------                  -----------------                      -----------------
    """

    @staticmethod
    @override(Executor)
    def _need_ffmpeg(asset):
        # 1) if quality width/height do not to agree with dis width/height,
        # must rely on ffmpeg for scaling
        # 2) if crop/pad/etc. is need, need ffmpeg
        # 3) if dis videos' start/end frames specified, need ffmpeg for
        # frame extraction
        # 4) if workfile_yuv_type specified and doesn't agree with the dis yuv_type
        ret = asset.quality_width_height != asset.dis_width_height \
               or asset.dis_yuv_type == 'notyuv' \
               or asset.dis_start_end_frame is not None \
               or 'workfile_yuv_type' in asset.asset_dict and asset.workfile_yuv_type != asset.dis_yuv_type
        for key in Asset.ORDERED_FILTER_LIST:
            ret = ret or asset.get_filter_cmd(key, 'dis') is not None

        return ret

    @classmethod
    def _assert_an_asset(cls, asset):

        # needed by _generate_result, and by _open_dis_workfile if called
        assert asset.quality_width_height is not None

        if cls._need_ffmpeg(asset):
            VmafExternalConfig.get_and_assert_ffmpeg()

        # if crop_cmd or pad_cmd or etc. is specified, make sure quality_width and
        # quality_height are EXPLICITLY specified in asset_dict
        if asset.dis_crop_cmd is not None:
            assert 'quality_width' in asset.asset_dict and 'quality_height' in asset.asset_dict, \
                'If crop_cmd etc. is specified, must also EXPLICITLY specify quality_width and quality_height.'
        if asset.dis_pad_cmd is not None:
            assert 'quality_width' in asset.asset_dict and 'quality_height' in asset.asset_dict, \
                'If pad_cmd etc. is specified, must also EXPLICITLY specify quality_width and quality_height.'

    @staticmethod
    def _get_workfile_yuv_type(asset):
        """ Same as original yuv type, unless it is notyuv or specified by the user; 
        in this case, use format as set at a higher level"""

        if 'workfile_yuv_type' in asset.asset_dict or asset.dis_yuv_type == 'notyuv':
            return asset.workfile_yuv_type
        else:
            return asset.dis_yuv_type

    @override(Executor)
    def _wait_for_workfiles(self, asset):
        # wait til workfile paths being generated
        for i in range(10):
            if os.path.exists(asset.dis_workfile_path):
                break
            sleep(0.1)
        else:
            raise RuntimeError("dis video workfile path {} is missing.".format(
                asset.dis_workfile_path))

    @override(Executor)
    def _wait_for_procfiles(self, asset):
        # wait til procfile paths being generated
        for i in range(10):
            if os.path.exists(asset.dis_procfile_path):
                break
            sleep(0.1)
        else:
            raise RuntimeError("dis video procfile path {} is missing.".format(
                asset.dis_procfile_path))

    @override(Executor)
    def _assert_paths(self, asset):
        assert os.path.exists(asset.dis_path) or match_any_files(asset.dis_path), \
            "Distorted path {} does not exist.".format(asset.dis_path)

    @override(Executor)
    def _open_workfiles(self, asset):
        self._open_dis_workfile(asset, fifo_mode=False)

    @override(Executor)
    def _open_workfiles_in_fifo_mode(self, asset):
        dis_p = multiprocessing.Process(target=self._open_dis_workfile,
                                        args=(asset, True))
        dis_p.start()
        self._wait_for_workfiles(asset)

    @override(Executor)
    def _open_procfiles(self, asset):
        self._open_dis_procfile(asset, fifo_mode=False)

    @override(Executor)
    def _open_procfiles_in_fifo_mode(self, asset):
        dis_p = multiprocessing.Process(target=self._open_dis_procfile,
                                        args=(asset, True))
        dis_p.start()
        self._wait_for_procfiles(asset)

    @override(Executor)
    def _close_workfiles(self, asset):
        self._close_dis_workfile(asset)

    @classmethod
    @override(Executor)
    def _close_procfiles(cls, asset):
        cls._close_dis_procfile(asset)

    @override(Executor)
    def _save_result(self, result):
        self.result_store.save(result)
        if self.save_workfiles:
            self.result_store.save_workfile(result, result.asset.dis_workfile_path, '_dis')
        return result
