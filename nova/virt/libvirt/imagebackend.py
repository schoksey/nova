# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 Grid Dynamics
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc
import contextlib
import os
import urllib

from oslo.config import cfg

from nova import exception
from nova.openstack.common import excutils
from nova.openstack.common import fileutils
from nova.openstack.common.gettextutils import _
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova import utils
from nova.virt.disk import api as disk
from nova.virt import images
from nova.virt.libvirt import config as vconfig
from nova.virt.libvirt import utils as libvirt_utils


try:
    import rados
    import rbd
except ImportError:
    rados = None
    rbd = None


__imagebackend_opts = [
    cfg.StrOpt('libvirt_images_type',
            default='default',
            help='VM Images format. Acceptable values are: raw, qcow2, lvm,'
                 'rbd, default. If default is specified,'
                 ' then use_cow_images flag is used instead of this one.'),
    cfg.StrOpt('libvirt_images_volume_group',
            help='LVM Volume Group that is used for VM images, when you'
                 ' specify libvirt_images_type=lvm.'),
    cfg.BoolOpt('libvirt_sparse_logical_volumes',
            default=False,
            help='Create sparse logical volumes (with virtualsize)'
                 ' if this flag is set to True.'),
    cfg.IntOpt('libvirt_lvm_snapshot_size',
            default=1000,
            help='The amount of storage (in megabytes) to allocate for LVM'
                    ' snapshot copy-on-write blocks.'),
    cfg.StrOpt('libvirt_images_rbd_pool',
            default='rbd',
            help='the RADOS pool in which rbd volumes are stored'),
    cfg.StrOpt('libvirt_images_rbd_ceph_conf',
            default='',  # default determined by librados
            help='path to the ceph configuration file to use'),
        ]

CONF = cfg.CONF
CONF.register_opts(__imagebackend_opts)
CONF.import_opt('base_dir_name', 'nova.virt.libvirt.imagecache')
CONF.import_opt('preallocate_images', 'nova.virt.driver')
CONF.import_opt('rbd_user', 'nova.virt.libvirt.volume')
CONF.import_opt('rbd_secret_uuid', 'nova.virt.libvirt.volume')

LOG = logging.getLogger(__name__)


class Image(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self, source_type, driver_format, is_block_dev=False):
        """Image initialization.

        :source_type: block or file
        :driver_format: raw or qcow2
        :is_block_dev:
        """
        self.source_type = source_type
        self.driver_format = driver_format
        self.is_block_dev = is_block_dev
        self.preallocate = False

        # NOTE(dripton): We store lines of json (path, disk_format) in this
        # file, for some image types, to prevent attacks based on changing the
        # disk_format.
        self.disk_info_path = None

        # NOTE(mikal): We need a lock directory which is shared along with
        # instance files, to cover the scenario where multiple compute nodes
        # are trying to create a base file at the same time
        self.lock_path = os.path.join(CONF.instances_path, 'locks')

    @abc.abstractmethod
    def create_image(self, prepare_template, base, size, *args, **kwargs):
        """Create image from template.

        Contains specific behavior for each image type.

        :prepare_template: function, that creates template.
        Should accept `target` argument.
        :base: Template name
        :size: Size of created image in bytes
        """
        pass

    def libvirt_info(self, disk_bus, disk_dev, device_type, cache_mode,
            extra_specs, hypervisor_version):
        """Get `LibvirtConfigGuestDisk` filled for this image.

        :disk_dev: Disk bus device name
        :disk_bus: Disk bus type
        :device_type: Device type for this image.
        :cache_mode: Caching mode for this image
        :extra_specs: Instance type extra specs dict.
        """
        info = vconfig.LibvirtConfigGuestDisk()
        info.source_type = self.source_type
        info.source_device = device_type
        info.target_bus = disk_bus
        info.target_dev = disk_dev
        info.driver_cache = cache_mode
        info.driver_format = self.driver_format
        driver_name = libvirt_utils.pick_disk_driver_name(hypervisor_version,
                                                          self.is_block_dev)
        info.driver_name = driver_name
        info.source_path = self.path

        tune_items = ['disk_read_bytes_sec', 'disk_read_iops_sec',
            'disk_write_bytes_sec', 'disk_write_iops_sec',
            'disk_total_bytes_sec', 'disk_total_iops_sec']
        # Note(yaguang): Currently, the only tuning available is Block I/O
        # throttling for qemu.
        if self.source_type in ['file', 'block']:
            for key, value in extra_specs.iteritems():
                scope = key.split(':')
                if len(scope) > 1 and scope[0] == 'quota':
                    if scope[1] in tune_items:
                        setattr(info, scope[1], value)
        return info

    def check_image_exists(self):
        return os.path.exists(self.path)

    def cache(self, fetch_func, filename, size=None, *args, **kwargs):
        """Creates image from template.

        Ensures that template and image not already exists.
        Ensures that base directory exists.
        Synchronizes on template fetching.

        :fetch_func: Function that creates the base image
                     Should accept `target` argument.
        :filename: Name of the file in the image directory
        :size: Size of created image in bytes (optional)
        """
        @utils.synchronized(filename, external=True, lock_path=self.lock_path)
        def call_if_not_exists(target, *args, **kwargs):
            if not os.path.exists(target):
                fetch_func(target=target, *args, **kwargs)
            elif CONF.libvirt_images_type == "lvm" and \
                    'ephemeral_size' in kwargs:
                fetch_func(target=target, *args, **kwargs)

        base_dir = os.path.join(CONF.instances_path, CONF.base_dir_name)
        if not os.path.exists(base_dir):
            fileutils.ensure_tree(base_dir)
        base = os.path.join(base_dir, filename)

        if not self.check_image_exists() or not os.path.exists(base):
            self.create_image(call_if_not_exists, base, size,
                              *args, **kwargs)

        if (size and self.preallocate and self._can_fallocate() and
                os.access(self.path, os.W_OK)):
            utils.execute('fallocate', '-n', '-l', size, self.path)

    def _can_fallocate(self):
        """Check once per class, whether fallocate(1) is available,
           and that the instances directory supports fallocate(2).
        """
        can_fallocate = getattr(self.__class__, 'can_fallocate', None)
        if can_fallocate is None:
            _out, err = utils.trycmd('fallocate', '-n', '-l', '1',
                                     self.path + '.fallocate_test')
            fileutils.delete_if_exists(self.path + '.fallocate_test')
            can_fallocate = not err
            self.__class__.can_fallocate = can_fallocate
            if not can_fallocate:
                LOG.error('Unable to preallocate_images=%s at path: %s' %
                          (CONF.preallocate_images, self.path))
        return can_fallocate

    def verify_base_size(self, base, size, base_size=0):
        """Check that the base image is not larger than size.
           Since images can't be generally shrunk, enforce this
           constraint taking account of virtual image size.
        """

        # Note(pbrady): The size and min_disk parameters of a glance
        #  image are checked against the instance size before the image
        #  is even downloaded from glance, but currently min_disk is
        #  adjustable and doesn't currently account for virtual disk size,
        #  so we need this extra check here.
        # NOTE(cfb): Having a flavor that sets the root size to 0 and having
        #  nova effectively ignore that size and use the size of the
        #  image is considered a feature at this time, not a bug.

        if size is None:
            return

        if size and not base_size:
            base_size = self.get_disk_size(base)

        if size < base_size:
            msg = _('%(base)s virtual size %(base_size)s '
                    'larger than flavor root disk size %(size)s')
            LOG.error(msg % {'base': base,
                              'base_size': base_size,
                              'size': size})
            raise exception.InstanceTypeDiskTooSmall()

    def get_disk_size(self, name):
        disk.get_disk_size(name)

    def snapshot_create(self):
        raise NotImplementedError()

    def snapshot_extract(self, target, out_format):
        raise NotImplementedError()

    def _get_driver_format(self):
        return self.driver_format

    def resolve_driver_format(self):
        """Return the driver format for self.path.

        First checks self.disk_info_path for an entry.
        If it's not there, calls self._get_driver_format(), and then
        stores the result in self.disk_info_path

        See https://bugs.launchpad.net/nova/+bug/1221190
        """
        def _dict_from_line(line):
            if not line:
                return {}
            try:
                return jsonutils.loads(line)
            except (TypeError, ValueError) as e:
                msg = (_("Could not load line %(line)s, got error "
                        "%(error)s") %
                        {'line': line, 'error': unicode(e)})
                raise exception.InvalidDiskInfo(reason=msg)

        @utils.synchronized(self.disk_info_path, external=False,
                            lock_path=self.lock_path)
        def write_to_disk_info_file():
            # Use os.open to create it without group or world write permission.
            fd = os.open(self.disk_info_path, os.O_RDWR | os.O_CREAT, 0o644)
            with os.fdopen(fd, "r+") as disk_info_file:
                line = disk_info_file.read().rstrip()
                dct = _dict_from_line(line)
                if self.path in dct:
                    msg = _("Attempted overwrite of an existing value.")
                    raise exception.InvalidDiskInfo(reason=msg)
                dct.update({self.path: driver_format})
                disk_info_file.seek(0)
                disk_info_file.truncate()
                disk_info_file.write('%s\n' % jsonutils.dumps(dct))
            # Ensure the file is always owned by the nova user so qemu can't
            # write it.
            utils.chown(self.disk_info_path, owner_uid=os.getuid())

        try:
            if (self.disk_info_path is not None and
                        os.path.exists(self.disk_info_path)):
                with open(self.disk_info_path) as disk_info_file:
                    line = disk_info_file.read().rstrip()
                    dct = _dict_from_line(line)
                    for path, driver_format in dct.iteritems():
                        if path == self.path:
                            return driver_format
            driver_format = self._get_driver_format()
            if self.disk_info_path is not None:
                fileutils.ensure_tree(os.path.dirname(self.disk_info_path))
                write_to_disk_info_file()
        except OSError as e:
            raise exception.DiskInfoReadWriteFail(reason=unicode(e))
        return driver_format

    def direct_fetch(self, image_id, image_meta, image_locations, max_size=0):
        """Create an image from a direct image location.

        :raises: exception.ImageUnacceptable if it cannot be fetched directly
        """
        reason = _('direct_fetch() is not implemented')
        raise exception.ImageUnacceptable(image_id=image_id, reason=reason)

    def direct_snapshot(self, snapshot_name, image_format, image_id):
        """Prepare a snapshot for direct reference from glance

        :raises: exception.ImageUnacceptable if it cannot be
                 referenced directly in the specified image format
        :returns: URL to be given to glance
        """
        reason = _('direct_snapshot() is not implemented')
        raise exception.ImageUnacceptable(image_id=image_id, reason=reason)


class Raw(Image):
    def __init__(self, instance=None, disk_name=None, path=None):
        super(Raw, self).__init__("file", "raw", is_block_dev=False)

        self.path = (path or
                     os.path.join(libvirt_utils.get_instance_path(instance),
                                  disk_name))
        self.preallocate = CONF.preallocate_images != 'none'
        self.disk_info_path = os.path.join(os.path.dirname(self.path),
                                           'disk.info')
        self.correct_format()

    def _get_driver_format(self):
        data = images.qemu_img_info(self.path)
        return data.file_format or 'raw'

    def correct_format(self):
        if os.path.exists(self.path):
            self.driver_format = self.resolve_driver_format()

    def create_image(self, prepare_template, base, size, *args, **kwargs):
        @utils.synchronized(base, external=True, lock_path=self.lock_path)
        def copy_raw_image(base, target, size):
            libvirt_utils.copy_image(base, target)
            if size:
                # class Raw is misnamed, format may not be 'raw' in all cases
                use_cow = self.driver_format == 'qcow2'
                disk.extend(target, size, use_cow=use_cow)

        generating = 'image_id' not in kwargs
        if generating:
            #Generating image in place
            prepare_template(target=self.path, *args, **kwargs)
        else:
            prepare_template(target=base, max_size=size, *args, **kwargs)
            self.verify_base_size(base, size)
            if not os.path.exists(self.path):
                with fileutils.remove_path_on_error(self.path):
                    copy_raw_image(base, self.path, size)
        self.correct_format()

    def snapshot_extract(self, target, out_format):
        images.convert_image(self.path, target, out_format)


class Qcow2(Image):
    def __init__(self, instance=None, disk_name=None, path=None):
        super(Qcow2, self).__init__("file", "qcow2", is_block_dev=False)

        self.path = (path or
                     os.path.join(libvirt_utils.get_instance_path(instance),
                                  disk_name))
        self.preallocate = CONF.preallocate_images != 'none'
        self.disk_info_path = os.path.join(os.path.dirname(self.path),
                                           'disk.info')
        self.resolve_driver_format()

    def create_image(self, prepare_template, base, size, *args, **kwargs):
        @utils.synchronized(base, external=True, lock_path=self.lock_path)
        def copy_qcow2_image(base, target, size):
            # TODO(pbrady): Consider copying the cow image here
            # with preallocation=metadata set for performance reasons.
            # This would be keyed on a 'preallocate_images' setting.
            libvirt_utils.create_cow_image(base, target)
            if size:
                disk.extend(target, size, use_cow=True)

        # Download the unmodified base image unless we already have a copy.
        if not os.path.exists(base):
            prepare_template(target=base, max_size=size, *args, **kwargs)
        else:
            self.verify_base_size(base, size)

        legacy_backing_size = None
        legacy_base = base

        # Determine whether an existing qcow2 disk uses a legacy backing by
        # actually looking at the image itself and parsing the output of the
        # backing file it expects to be using.
        if os.path.exists(self.path):
            backing_path = libvirt_utils.get_disk_backing_file(self.path)
            if backing_path is not None:
                backing_file = os.path.basename(backing_path)
                backing_parts = backing_file.rpartition('_')
                if backing_file != backing_parts[-1] and \
                        backing_parts[-1].isdigit():
                    legacy_backing_size = int(backing_parts[-1])
                    legacy_base += '_%d' % legacy_backing_size
                    legacy_backing_size *= 1024 * 1024 * 1024

        # Create the legacy backing file if necessary.
        if legacy_backing_size:
            if not os.path.exists(legacy_base):
                with fileutils.remove_path_on_error(legacy_base):
                    libvirt_utils.copy_image(base, legacy_base)
                    disk.extend(legacy_base, legacy_backing_size, use_cow=True)

        if not os.path.exists(self.path):
            with fileutils.remove_path_on_error(self.path):
                copy_qcow2_image(base, self.path, size)

    def snapshot_extract(self, target, out_format):
        libvirt_utils.extract_snapshot(self.path, 'qcow2',
                                       target,
                                       out_format)


class Lvm(Image):
    @staticmethod
    def escape(filename):
        return filename.replace('_', '__')

    def __init__(self, instance=None, disk_name=None, path=None):
        super(Lvm, self).__init__("block", "raw", is_block_dev=True)

        if path:
            info = libvirt_utils.logical_volume_info(path)
            self.vg = info['VG']
            self.lv = info['LV']
            self.path = path
        else:
            if not CONF.libvirt_images_volume_group:
                raise RuntimeError(_('You should specify'
                                     ' libvirt_images_volume_group'
                                     ' flag to use LVM images.'))
            self.vg = CONF.libvirt_images_volume_group
            self.lv = '%s_%s' % (self.escape(instance['name']),
                                 self.escape(disk_name))
            self.path = os.path.join('/dev', self.vg, self.lv)

        # TODO(pbrady): possibly deprecate libvirt_sparse_logical_volumes
        # for the more general preallocate_images
        self.sparse = CONF.libvirt_sparse_logical_volumes
        self.preallocate = not self.sparse

    def _can_fallocate(self):
        return False

    def create_image(self, prepare_template, base, size, *args, **kwargs):
        @utils.synchronized(base, external=True, lock_path=self.lock_path)
        def create_lvm_image(base, size):
            base_size = disk.get_disk_size(base)
            self.verify_base_size(base, size, base_size=base_size)
            resize = size > base_size
            size = size if resize else base_size
            libvirt_utils.create_lvm_image(self.vg, self.lv,
                                           size, sparse=self.sparse)
            images.convert_image(base, self.path, 'raw', run_as_root=True)
            if resize:
                disk.resize2fs(self.path, run_as_root=True)

        generated = 'ephemeral_size' in kwargs

        #Generate images with specified size right on volume
        if generated and size:
            libvirt_utils.create_lvm_image(self.vg, self.lv,
                                           size, sparse=self.sparse)
            with self.remove_volume_on_error(self.path):
                prepare_template(target=self.path, *args, **kwargs)
        else:
            prepare_template(target=base, max_size=size, *args, **kwargs)
            with self.remove_volume_on_error(self.path):
                create_lvm_image(base, size)

    @contextlib.contextmanager
    def remove_volume_on_error(self, path):
        try:
            yield
        except Exception:
            with excutils.save_and_reraise_exception():
                libvirt_utils.remove_logical_volumes(path)

    def snapshot_extract(self, target, out_format):
        images.convert_image(self.path, target, out_format,
                             run_as_root=True)


class RBDVolumeProxy(object):
    """Context manager for dealing with an existing rbd volume.

    This handles connecting to rados and opening an ioctx automatically, and
    otherwise acts like a librbd Image object.

    The underlying librados client and ioctx can be accessed as the attributes
    'client' and 'ioctx'.
    """
    def __init__(self, driver, name, pool=None, snapshot=None,
                 read_only=False):
        client, ioctx = driver._connect_to_rados(pool)
        try:
            self.volume = driver.rbd.Image(ioctx, str(name),
                                           snapshot=libvirt_utils.ascii_str(
                                                        snapshot),
                                           read_only=read_only)
        except driver.rbd.Error:
            LOG.exception(_("error opening rbd image %s"), name)
            driver._disconnect_from_rados(client, ioctx)
            raise
        self.driver = driver
        self.client = client
        self.ioctx = ioctx

    def __enter__(self):
        return self

    def __exit__(self, type_, value, traceback):
        try:
            self.volume.close()
        finally:
            self.driver._disconnect_from_rados(self.client, self.ioctx)

    def __getattr__(self, attrib):
        return getattr(self.volume, attrib)


class RADOSClient(object):
    """Context manager to simplify error handling for connecting to ceph."""
    def __init__(self, driver, pool=None):
        self.driver = driver
        self.cluster, self.ioctx = driver._connect_to_rados(pool)

    def __enter__(self):
        return self

    def __exit__(self, type_, value, traceback):
        self.driver._disconnect_from_rados(self.cluster, self.ioctx)


class Rbd(Image):
    def __init__(self, instance=None, disk_name=None, path=None,
                 snapshot_name=None, **kwargs):
        super(Rbd, self).__init__("block", 'raw', is_block_dev=True)
        if path:
            try:
                self.rbd_name = str(path.split('/')[1])
            except IndexError:
                raise exception.InvalidDevicePath(path=path)
        else:
            self.rbd_name = str('%s_%s' % (instance['uuid'], disk_name))
        self.snapshot_name = snapshot_name
        if not CONF.libvirt_images_rbd_pool:
            raise RuntimeError(_('You should specify'
                                 ' libvirt_images_rbd_pool'
                                 ' flag to use rbd images.'))
        self.pool = str(CONF.libvirt_images_rbd_pool)
        self.ceph_conf = libvirt_utils.ascii_str(
                         CONF.libvirt_images_rbd_ceph_conf)
        self.rbd_user = libvirt_utils.ascii_str(CONF.rbd_user)
        self.rbd = kwargs.get('rbd', rbd)
        self.rados = kwargs.get('rados', rados)

        self.path = 'rbd:%s/%s' % (self.pool, self.rbd_name)
        if self.rbd_user:
            self.path += ':id=' + self.rbd_user
        if self.ceph_conf:
            self.path += ':conf=' + self.ceph_conf

    def _connect_to_rados(self, pool=None):
        client = self.rados.Rados(rados_id=self.rbd_user,
                                  conffile=self.ceph_conf)
        try:
            client.connect()
            pool_to_open = str(pool or self.pool)
            ioctx = client.open_ioctx(pool_to_open)
            return client, ioctx
        except self.rados.Error:
            # shutdown cannot raise an exception
            client.shutdown()
            raise

    def _disconnect_from_rados(self, client, ioctx):
        # closing an ioctx cannot raise an exception
        ioctx.close()
        client.shutdown()

    def _supports_layering(self):
        return hasattr(self.rbd, 'RBD_FEATURE_LAYERING')

    def _ceph_args(self):
        args = []
        args.extend(['--id', self.rbd_user])
        args.extend(['--conf', self.ceph_conf])
        return args

    def _get_mon_addrs(self):
        args = ['ceph', 'mon', 'dump', '--format=json'] + self._ceph_args()
        out, _ = utils.execute(*args)
        lines = out.split('\n')
        if lines[0].startswith('dumped monmap epoch'):
            lines = lines[1:]
        monmap = jsonutils.loads('\n'.join(lines))
        addrs = [mon['addr'] for mon in monmap['mons']]
        hosts = []
        ports = []
        for addr in addrs:
            host_port = addr[:addr.rindex('/')]
            host, port = host_port.rsplit(':', 1)
            hosts.append(host.strip('[]'))
            ports.append(port)
        return hosts, ports

    def libvirt_info(self, disk_bus, disk_dev, device_type, cache_mode,
            extra_specs, hypervisor_version):
        """Get `LibvirtConfigGuestDisk` filled for this image.

        :disk_dev: Disk bus device name
        :disk_bus: Disk bus type
        :device_type: Device type for this image.
        :cache_mode: Caching mode for this image
        :extra_specs: Instance type extra specs dict.
        """
        info = vconfig.LibvirtConfigGuestDisk()

        hosts, ports = self._get_mon_addrs()
        info.source_device = device_type
        info.driver_format = 'raw'
        info.driver_cache = cache_mode
        info.target_bus = disk_bus
        info.target_dev = disk_dev
        info.source_type = 'network'
        info.source_protocol = 'rbd'
        info.source_name = '%s/%s' % (self.pool, self.rbd_name)
        info.source_hosts = hosts
        info.source_ports = ports
        auth_enabled = (CONF.rbd_user is not None)
        if CONF.rbd_secret_uuid:
            info.auth_secret_uuid = CONF.rbd_secret_uuid
            auth_enabled = True  # Force authentication locally
            if CONF.rbd_user:
                info.auth_username = CONF.rbd_user
        if auth_enabled:
            info.auth_secret_type = 'ceph'
            info.auth_secret_uuid = CONF.rbd_secret_uuid
        return info

    def _can_fallocate(self):
        return False

    def check_image_exists(self):
        rbd_volumes = libvirt_utils.list_rbd_volumes(self.pool)
        for vol in rbd_volumes:
            if vol.startswith(self.rbd_name):
                return True

        return False

    def _resize(self, size):
        with RBDVolumeProxy(self, self.rbd_name) as vol:
            vol.resize(int(size))

    def _size(self):
        return self.get_disk_size(self.rbd_name)

    def get_disk_size(self, name):
        with RBDVolumeProxy(self, name) as vol:
            return vol.size()

    def create_image(self, prepare_template, base, size, *args, **kwargs):
        if self.rbd is None:
            raise RuntimeError(_('rbd python libraries not found'))

        old_format = True
        features = 0
        if self._supports_layering():
            old_format = False
            features = self.rbd.RBD_FEATURE_LAYERING

        if not self.check_image_exists():
            prepare_template(target=base, max_size=size, *args, **kwargs)
        else:
            self.verify_base_size(base, size)

        # prepare_template may have created the image via direct_fetch()
        if not self.check_image_exists():
            # keep using the command line import instead of librbd since it
            # detects zeroes to preserve sparseness in the image
            args = ['--pool', self.pool, base, self.rbd_name]
            if self._supports_layering():
                args += ['--new-format']
            args += self._ceph_args()
            libvirt_utils.import_rbd_image(*args)

        if size and self._size() < size:
            self._resize(size)

    def snapshot_extract(self, target, out_format):
        images.convert_image(self.path, target, out_format)

    def snapshot_delete(self):
        pass

    def _parse_location(self, location):
        prefix = 'rbd://'
        if not location.startswith(prefix):
            reason = _('Not stored in rbd')
            raise exception.ImageUnacceptable(image_id=location, reason=reason)
        pieces = map(urllib.unquote, location[len(prefix):].split('/'))
        if any(map(lambda p: p == '', pieces)):
            reason = _('Blank components')
            raise exception.ImageUnacceptable(image_id=location, reason=reason)
        if len(pieces) != 4:
            reason = _('Not an rbd snapshot')
            raise exception.ImageUnacceptable(image_id=location, reason=reason)
        return pieces

    def _get_fsid(self):
        with RADOSClient(self) as client:
            return client.cluster.get_fsid()

    def _is_cloneable(self, image_location):
        try:
            fsid, pool, image, snapshot = self._parse_location(image_location)
        except exception.ImageUnacceptable as e:
            LOG.debug(_('not cloneable: %s'), e)
            return False

        if self._get_fsid() != fsid:
            reason = _('%s is in a different ceph cluster') % image_location
            LOG.debug(reason)
            return False

        # check that we can read the image
        try:
            with RBDVolumeProxy(self, image,
                                pool=pool,
                                snapshot=snapshot,
                                read_only=True):
                return True
        except self.rbd.Error as e:
            LOG.debug(_('Unable to open image %(loc)s: %(err)s') %
                      dict(loc=image_location, err=e))
            return False

    def _clone(self, pool, image, snapshot, clone_name):
        with RADOSClient(self, str(pool)) as src_client:
            with RADOSClient(self) as dest_client:
                self.rbd.RBD().clone(src_client.ioctx,
                                     str(image),
                                     str(snapshot),
                                     dest_client.ioctx,
                                     clone_name,
                                     features=self.rbd.RBD_FEATURE_LAYERING)

    def direct_fetch(self, image_id, image_meta, image_locations, max_size=0):
        if self.check_image_exists():
            return
        if image_meta.get('disk_format') not in ['raw', 'iso']:
            reason = _('Image is not raw format')
            raise exception.ImageUnacceptable(image_id=image_id, reason=reason)
        if not self._supports_layering():
            reason = _('installed version of librbd does not support cloning')
            raise exception.ImageUnacceptable(image_id=image_id, reason=reason)

        for location in image_locations:
            url = location['url']
            if self._is_cloneable(url):
                prefix, pool, image, snapshot = self._parse_location(url)
                return self._clone(pool, image, snapshot, self.rbd_name)

        reason = _('No image locations are accessible')
        raise exception.ImageUnacceptable(image_id=image_id, reason=reason)

    def _create_snapshot(self, name, snap_name):
        """Creates an rbd snapshot."""
        with RBDVolumeProxy(self, name) as volume:
            snap = snap_name.encode('utf-8')
            volume.create_snap(snap)
            if self._supports_layering():
                volume.protect_snap(snap)

    def _delete_snapshot(self, name, snap_name):
        """Deletes an rbd snapshot."""
        # NOTE(dosaboy): this was broken by commit cbe1d5f. Ensure names are
        #                utf-8 otherwise librbd will barf.
        snap = snap_name.encode('utf-8')
        with RBDVolumeProxy(self, name) as volume:
            if self._supports_layering():
                try:
                    volume.unprotect_snap(snap)
                except rbd.ImageBusy:
                    raise exception.SnapshotIsBusy(snapshot_name=snap)
            volume.remove_snap(snap)

    def direct_snapshot(self, snapshot_name, image_format, image_id):
        deletion_marker = '_to_be_deleted_by_glance'
        if image_format != 'raw':
            reason = _('only raw format is supported')
            raise exception.ImageUnacceptable(image_id=image_id, reason=reason)
        if not self._supports_layering():
            reason = _('librbd is too old')
            raise exception.ImageUnacceptable(image_id=image_id, reason=reason)
        rbd_snap_name = snapshot_name + deletion_marker
        self._create_snapshot(self.rbd_name, rbd_snap_name)
        clone_name = self.rbd_name + '_clone_' + snapshot_name
        clone_snap = 'snap'
        self._clone(self.pool, self.rbd_name, rbd_snap_name, clone_name)
        self._create_snapshot(clone_name, clone_snap)
        fsid = self._get_fsid()
        return 'rbd://{fsid}/{pool}/{image}/{snap}'.format(
            fsid=fsid,
            pool=self.pool,
            image=clone_name,
            snap=clone_snap)


class Backend(object):
    def __init__(self, use_cow):
        self.BACKEND = {
            'raw': Raw,
            'qcow2': Qcow2,
            'lvm': Lvm,
            'rbd': Rbd,
            'default': Qcow2 if use_cow else Raw
        }

    def backend(self, image_type=None):
        if not image_type:
            image_type = CONF.libvirt_images_type
        image = self.BACKEND.get(image_type)
        if not image:
            raise RuntimeError(_('Unknown image_type=%s') % image_type)
        return image

    def image(self, instance, disk_name, image_type=None):
        """Constructs image for selected backend

        :instance: Instance name.
        :name: Image name.
        :image_type: Image type.
        Optional, is CONF.libvirt_images_type by default.
        """
        backend = self.backend(image_type)
        return backend(instance=instance, disk_name=disk_name)

    def snapshot(self, disk_path, image_type=None):
        """Returns snapshot for given image

        :path: path to image
        :image_type: type of image
        """
        backend = self.backend(image_type)
        return backend(path=disk_path)
