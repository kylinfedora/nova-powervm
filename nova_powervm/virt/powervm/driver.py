# Copyright 2014, 2015 IBM Corp.
#
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

from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.console import type as console_type
from nova import context as ctx
from nova import exception
from nova import image
from nova.i18n import _LI, _LW, _
from nova.objects import flavor as flavor_obj
from nova import utils as n_utils
from nova.virt import configdrive
from nova.virt import driver
import re
import time

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import importutils
import taskflow.engines
from taskflow.patterns import linear_flow as lf
import taskflow.task

from pypowervm import adapter as pvm_apt
from pypowervm import exceptions as pvm_exc
from pypowervm.helpers import log_helper as log_hlp
from pypowervm.tasks import power as pvm_pwr
from pypowervm.tasks import vterm as pvm_vterm
from pypowervm import util as pvm_util
from pypowervm.utils import retry as pvm_retry
from pypowervm.wrappers import managed_system as pvm_ms

from nova_powervm.virt.powervm.disk import driver as disk_dvr
from nova_powervm.virt.powervm import host as pvm_host
from nova_powervm.virt.powervm.tasks import storage as tf_stg
from nova_powervm.virt.powervm.tasks import vm as tf_vm
from nova_powervm.virt.powervm import vm
from nova_powervm.virt.powervm import volume as vol_attach

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

# Defines, for all cinder volume types, which volume driver to use.  Currently
# only supports Fibre Channel, which has multiple options for connections.
# The connection strategy is defined above.
VOLUME_DRIVER_MAPPINGS = {
    'fibre_channel': vol_attach.FC_STRATEGY_MAPPING[CONF.fc_attach_strategy]
}

DISK_ADPT_NS = 'nova_powervm.virt.powervm.disk'
DISK_ADPT_MAPPINGS = {
    'localdisk': 'localdisk.LocalStorage',
    'ssp': 'ssp.SSPDiskAdapter'
}


class PowerVMDriver(driver.ComputeDriver):

    """PowerVM Implementation of Compute Driver."""

    def __init__(self, virtapi):
        super(PowerVMDriver, self).__init__(virtapi)

    def init_host(self, host):
        """Initialize anything that is necessary for the driver to function,
        including catching up with currently running VM's on the given host.
        """

        # Get an adapter
        self._get_adapter()
        # First need to resolve the managed host UUID
        self._get_host_uuid()
        # Initialize the UUID Cache. Lets not prime it at this time.
        vm.UUIDCache(self.adapter)

        # Initialize the disk adapter.  Sets self.disk_drv
        self._get_disk_adapter()
        self.image_api = image.API()

        # Initialize the volume drivers
        self.vol_drvs = _inst_dict(VOLUME_DRIVER_MAPPINGS)

        LOG.info(_LI("The compute driver has been initialized."))

    def _get_adapter(self):
        self.session = pvm_apt.Session()
        self.adapter = pvm_apt.Adapter(self.session,
                                       helpers=log_hlp.log_helper)

    def _get_disk_adapter(self):
        conn_info = {'adapter': self.adapter, 'host_uuid': self.host_uuid}

        self.disk_dvr = importutils.import_object_ns(
            DISK_ADPT_NS, DISK_ADPT_MAPPINGS[CONF.disk_driver], conn_info)

    def _get_host_uuid(self):
        """Get the System wrapper and its UUID for the (single) host."""
        syswraps = pvm_ms.System.wrap(
            self.adapter.read(pvm_ms.System.schema_type))
        if len(syswraps) != 1:
            raise Exception(
                _("Expected exactly one host; found %d"), len(syswraps))
        self.host_wrapper = syswraps[0]
        self.host_uuid = self.host_wrapper.uuid
        LOG.info(_LI("Host UUID is:%s") % self.host_uuid)

    @staticmethod
    def _log_operation(op, instance):
        """Log entry point of driver operations
        """
        LOG.info(_LI('Operation: %(op)s. Virtual machine display name: '
                     '%(display_name)s, name: %(name)s, UUID: %(uuid)s') %
                 {'op': op, 'display_name': instance.display_name,
                  'name': instance.name, 'uuid': instance.uuid})

    def get_info(self, instance):
        """Get the current status of an instance, by name (not ID!)

        Returns a dict containing:

        :state:           the running state, one of the power_state codes
        :max_mem:         (int) the maximum memory in KBytes allowed
        :mem:             (int) the memory in KBytes used by the domain
        :num_cpu:         (int) the number of virtual CPUs for the domain
        :cpu_time:        (int) the CPU time used in nanoseconds
        """
        info = vm.InstanceInfo(self.adapter, instance.name,
                               vm.get_pvm_uuid(instance))
        return info

    def list_instances(self):
        """Return the names of all the instances known to the virtualization
        layer, as a list.
        """
        lpar_list = vm.get_lpar_list(self.adapter, self.host_uuid)
        return lpar_list

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None,
              flavor=None):
        """Create a new instance/VM/domain on the virtualization platform.

        Once this successfully completes, the instance should be
        running (power_state.RUNNING).

        If this fails, any partial instance should be completely
        cleaned up, and the virtualization platform should be in the state
        that it was before this call began.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
                         This function should use the data there to guide
                         the creation of the new instance.
        :param image_meta: image object returned by nova.image.glance that
                           defines the image from which to boot this instance
        :param injected_files: User files to inject into instance.
        :param admin_password: Administrator password to set in instance.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: Information about block devices to be
                                  attached to the instance.
        :param flavor: The flavor for the instance to be spawned.
        """
        self._log_operation('spawn', instance)
        if not flavor:
            admin_ctx = ctx.get_admin_context(read_deleted='yes')
            flavor = (
                flavor_obj.Flavor.get_by_id(admin_ctx,
                                            instance.instance_type_id))

        is_boot_from_volume = (image_meta.get('id') is None)

        # Define the flow
        flow = lf.Flow("spawn")

        # Create the LPAR
        flow.add(tf_vm.Create(self.adapter, self.host_wrapper, instance,
                              flavor))

        # Plug the VIFs
        vif_plug_info = {'instance': instance, 'network_info': network_info}
        flow.add(taskflow.task.FunctorTask(self._plug_vifs, name='plug_vifs',
                                           inject=vif_plug_info))

        # Only add the image disk if this is from Glance.
        if not is_boot_from_volume:
            # Creates the boot image.
            flow.add(tf_stg.CreateDiskForImg(
                self.disk_dvr, context, instance, image_meta,
                disk_size=flavor.root_gb))

            # Connects up the disk to the LPAR
            flow.add(tf_stg.ConnectDisk(self.disk_dvr, context, instance))

        # Determine if there are volumes to connect.  If so, add a connection
        # for each type.
        bdms = self._extract_bdm(block_device_info)
        if bdms is not None:
            for bdm in bdms:
                conn_info = bdm.get('connection_info')
                drv_type = conn_info.get('driver_volume_type')
                vol_drv = self.vol_drvs.get(drv_type)
                flow.add(tf_stg.ConnectVolume(self.adapter, vol_drv, instance,
                                              conn_info, self.host_uuid))

        # If the config drive is needed, add those steps.
        if configdrive.required_by(instance):
            flow.add(tf_stg.CreateAndConnectCfgDrive(self.adapter,
                                                     self.host_uuid,
                                                     instance, injected_files,
                                                     network_info,
                                                     admin_password))

        # Last step is to power on the system.
        # Note: If moving to a Graph Flow, will need to change to depend on
        # the prior step.
        flow.add(tf_vm.PowerOn(self.adapter, self.host_uuid, instance))

        # Build the engine & run!
        engine = taskflow.engines.load(flow)
        engine.run()

    def destroy(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, migrate_data=None):
        """Destroy (shutdown and delete) the specified instance.

        If the instance is not found (for example if networking failed), this
        function should still succeed.  It's probably a good idea to log a
        warning in that case.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: Information about block devices that should
                                  be detached from the instance.
        :param destroy_disks: Indicates if disks should be destroyed
        :param migrate_data: implementation specific params

        """

        def _run_flow():
            # Define the flow
            flow = lf.Flow("destroy")

            # Power Off the LPAR
            flow.add(tf_vm.PowerOff(self.adapter, self.host_uuid,
                                    pvm_inst_uuid, instance))

            # Delete the virtual optical
            flow.add(tf_stg.DeleteVOpt(self.adapter, self.host_uuid, instance,
                                       pvm_inst_uuid))

            # Determine if there are volumes to disconnect.  If so, remove each
            # volume
            bdms = self._extract_bdm(block_device_info)
            if bdms is not None:
                for bdm in bdms:
                    conn_info = bdm.get('connection_info')
                    drv_type = conn_info.get('driver_volume_type')
                    vol_drv = self.vol_drvs.get(drv_type)
                    flow.add(tf_stg.DisconnectVolume(self.adapter, vol_drv,
                                                     instance, conn_info,
                                                     self.host_uuid,
                                                     pvm_inst_uuid))

            # Detach the disk storage adapters
            flow.add(tf_stg.DetachDisk(self.disk_dvr, context, instance,
                                       pvm_inst_uuid))

            # Delete the storage disks
            if destroy_disks:
                flow.add(tf_stg.DeleteDisk(self.disk_dvr, context, instance))

            # Last step is to delete the LPAR from the system.
            # Note: If moving to a Graph Flow, will need to change to depend on
            # the prior step.
            flow.add(tf_vm.Delete(self.adapter, pvm_inst_uuid, instance))

            # Build the engine & run!
            engine = taskflow.engines.load(flow)
            engine.run()

        self._log_operation('destroy', instance)
        if instance.task_state == task_states.RESIZE_REVERTING:
            # This destroy is part of resize, just skip destroying
            # TODO(IBM): What to do longer term
            LOG.info(_LI('Ignoring destroy call during resize revert.'))
            return

        try:
            pvm_inst_uuid = vm.get_pvm_uuid(instance)
            _run_flow()
        except exception.InstanceNotFound:
            LOG.warn(_LW('VM was not found during destroy operation.'),
                     instance=instance)
            return
        except pvm_exc.HttpError as e:
            # See if we were operating on the LPAR that we're deleting
            # and it wasn't found
            resp = e.response
            exp = '/ManagedSystem/.*/LogicalPartition/.*-.*-.*-.*-.*'
            if (resp.status == 404 and re.search(exp, resp.reqpath)):
                # It's the LPAR, so just return.
                LOG.warn(_LW('VM was not found during destroy operation.'),
                         instance=instance)
                return
            else:
                raise

        finally:
            # Delete the lpar from the cache so if it gets rebuilt it won't
            # have the old lpar uuid.
            vm.UUIDCache.get_cache().remove(instance.name)

    def attach_volume(self, context, connection_info, instance, mountpoint,
                      disk_bus=None, device_type=None, encryption=None):
        """Attach the volume to the instance at mountpoint using info."""
        self._log_operation('attach_volume', instance)

        # Define the flow
        flow = lf.Flow("attach_volume")

        # Get the LPAR Wrapper
        flow.add(tf_vm.Get(self.adapter, self.host_uuid, instance))

        # Determine if there are volumes to connect.  If so, add a connection
        # for each type.
        drv_type = connection_info.get('driver_volume_type')
        vol_drv = self.vol_drvs.get(drv_type)
        flow.add(tf_stg.ConnectVolume(self.adapter, vol_drv, instance,
                                      connection_info, self.host_uuid))

        # Build the engine & run!
        engine = taskflow.engines.load(flow)
        engine.run()

    def detach_volume(self, connection_info, instance, mountpoint,
                      encryption=None):
        """Detach the volume attached to the instance."""
        self._log_operation('detach_volume', instance)

        # Define the flow
        flow = lf.Flow("detach_volume")

        # Determine if there are volumes to connect.  If so, add a connection
        # for each type.
        drv_type = connection_info.get('driver_volume_type')
        vol_drv = self.vol_drvs.get(drv_type)
        pvm_inst_uuid = vm.get_pvm_uuid(instance)
        flow.add(tf_stg.DisconnectVolume(self.adapter, vol_drv, instance,
                                         connection_info, self.host_uuid,
                                         pvm_inst_uuid))

        # Build the engine & run!
        engine = taskflow.engines.load(flow)
        engine.run()

    def snapshot(self, context, instance, image_id, update_task_state):
        """Snapshots the specified instance.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param image_id: Reference to a pre-created image that will
                         hold the snapshot.
        """
        self._log_operation('snapshot', instance)
        # TODO(IBM): Implement snapshot

    def rescue(self, context, instance, network_info, image_meta,
               rescue_password):
        """Rescue the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        self._log_operation('rescue', instance)

        # We need the image size, which isn't in the system meta data
        # so get the all the info.
        image_meta = self.image_api.get(context, image_meta['id'])

        pvm_inst_uuid = vm.get_pvm_uuid(instance)
        # Define the flow
        flow = lf.Flow("rescue")

        # Get the LPAR Wrapper
        flow.add(tf_vm.Get(self.adapter, self.host_uuid, instance))

        # Power Off the LPAR
        flow.add(tf_vm.PowerOff(self.adapter, self.host_uuid,
                                pvm_inst_uuid, instance))

        # Creates the boot image.
        flow.add(tf_stg.CreateDiskForImg(
            self.disk_dvr, context, instance, image_meta,
            image_type=disk_dvr.DiskType.RESCUE))

        # Connects up the disk to the LPAR
        flow.add(tf_stg.ConnectDisk(self.disk_dvr, context, instance))

        # Last step is to power on the system.
        # TODO(IBM): Currently, sending the bootmode=sms options causes
        # the poweron job to fail.  Bypass it for now.  The VM can be
        # powered on manually to sms.
        # flow.add(tf_vm.PowerOn(self.adapter, self.host_uuid,
        #                       instance, pwr_opts=dict(bootmode='sms')))

        # Build the engine & run!
        engine = taskflow.engines.load(flow)
        engine.run()

    def unrescue(self, instance, network_info):
        """Unrescue the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        self._log_operation('unrescue', instance)

        pvm_inst_uuid = vm.get_pvm_uuid(instance)
        context = ctx.get_admin_context()

        # Define the flow
        flow = lf.Flow("unrescue")

        # Get the LPAR Wrapper
        flow.add(tf_vm.Get(self.adapter, self.host_uuid, instance))

        # Power Off the LPAR
        flow.add(tf_vm.PowerOff(self.adapter, self.host_uuid,
                                pvm_inst_uuid, instance))

        # Detach the disk adapter for the rescue image
        flow.add(tf_stg.DetachDisk(self.disk_dvr, context, instance,
                                   pvm_inst_uuid,
                                   disk_type=[disk_dvr.DiskType.RESCUE]))

        # Delete the storage disk for the rescue image
        flow.add(tf_stg.DeleteDisk(self.disk_dvr, context, instance))

        # Last step is to power on the system.
        flow.add(tf_vm.PowerOn(self.adapter, self.host_uuid, instance))

        # Build the engine & run!
        engine = taskflow.engines.load(flow)
        engine.run()

    def power_off(self, instance, timeout=0, retry_interval=0):
        """Power off the specified instance.

        :param instance: nova.objects.instance.Instance
        :param timeout: time to wait for GuestOS to shutdown
        :param retry_interval: How often to signal guest while
                               waiting for it to shutdown
        """
        self._log_operation('power_off', instance)
        vm.power_off(self.adapter, instance, self.host_uuid)

    def power_on(self, context, instance, network_info,
                 block_device_info=None):
        """Power on the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        self._log_operation('power_on', instance)
        vm.power_on(self.adapter, instance, self.host_uuid)

    def reboot(self, context, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None):
        """Reboot the specified instance.

        After this is called successfully, the instance's state
        goes back to power_state.RUNNING. The virtualization
        platform should ensure that the reboot action has completed
        successfully even in cases in which the underlying domain/vm
        is paused or halted/stopped.

        :param instance: nova.objects.instance.Instance
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param reboot_type: Either a HARD or SOFT reboot
        :param block_device_info: Info pertaining to attached volumes
        :param bad_volumes_callback: Function to handle any bad volumes
            encountered
        """
        self._log_operation(reboot_type + ' reboot', instance)
        force_immediate = reboot_type == 'HARD'
        entry = vm.get_instance_wrapper(self.adapter, instance, self.host_uuid)
        # Note: We're bypassing vm.power_off/_on because we don't want the
        # state checks imposed thereby.
        pvm_pwr.power_off(entry, self.host_uuid,
                          force_immediate=force_immediate)
        # pypowervm does NOT throw an exception if "already down".  Any other
        # exception from pypowervm is a legitimate failure; let it raise up.
        # If we get here, pypowervm thinks the instance is down.
        pvm_pwr.power_on(entry, self.host_uuid)
        # Again, pypowervm exceptions are sufficient to indicate real failure.
        # Otherwise, pypowervm thinks the instance is up.
        return True

    def get_available_resource(self, nodename):
        """Retrieve resource information.

        This method is called when nova-compute launches, and
        as part of a periodic task

        :param nodename:
            node which the caller want to get resources from
            a driver that manages only one node can safely ignore this
        :returns: Dictionary describing resources
        """

        resp = self.adapter.read(pvm_ms.System.schema_type,
                                 root_id=self.host_uuid)
        if resp:
            self.host_wrapper = pvm_ms.System.wrap(resp.entry)
        # Get host information
        data = pvm_host.build_host_resource_from_ms(self.host_wrapper)

        # Add the disk information
        data["local_gb"] = self.disk_dvr.capacity
        data["local_gb_used"] = self.disk_dvr.capacity_used

        return data

    def get_host_uptime(self):
        """Returns the result of calling "uptime" on the target host."""
        # trivial implementation from libvirt/driver.py for consistency
        out, err = n_utils.execute('env', 'LANG=C', 'uptime')
        return out

    def plug_vifs(self, instance, network_info):
        """Plug VIFs into networks."""
        self._log_operation('plug_vifs', instance)
        self._plug_vifs(instance, network_info)

    def _plug_vifs(self, instance, network_info):
        """Actual logic to plug VIFs into networks."""
        # Get all the current VIFs.  Only create new ones.
        cna_w_list = vm.get_cnas(self.adapter, instance, self.host_uuid)
        for vif in network_info:
            crt_cna = True
            for cna_w in cna_w_list:
                if cna_w.mac == pvm_util.sanitize_mac_for_api(vif['address']):
                    crt_cna = False

            # If the crt_cna flag is true, then actually kick off the create
            if crt_cna:
                LOG.info(_LI('Creating VIF with mac %(mac)s for instance '
                             '%(inst)s') % {'mac': vif['address'],
                                            'inst': instance.name},
                         instance=instance)
                vm.crt_vif(self.adapter, instance, self.host_uuid, vif)

        # Determine if we need to create the secure RMC VIF.  This should only
        # be needed if there is not a VIF on the secure RMC vSwitch
        vswitch_w = vm.get_secure_rmc_vswitch(self.adapter, self.host_uuid)
        if vswitch_w is not None:
            # If the vSwitch had been none, we can't create the VIF.  This
            # next check verifies that there are no existing NICs on the
            # vSwitch, so that the VM does not end up with multiple RMC VIFs.
            must_create_rmc_conn = True
            for cna_w in cna_w_list:
                if cna_w.vswitch_uri == vswitch_w.href:
                    must_create_rmc_conn = False
                    break

            if must_create_rmc_conn:
                vm.crt_secure_rmc_vif(self.adapter, instance, self.host_uuid)

    def unplug_vifs(self, instance, network_info):
        """Unplug VIFs from networks."""
        self._log_operation('unplug_vifs', instance)
        # TODO(IBM): Implement

    def get_available_nodes(self, refresh=False):
        """Returns nodenames of all nodes managed by the compute service.

        This method is for multi compute-nodes support. If a driver supports
        multi compute-nodes, this method returns a list of nodenames managed
        by the service. Otherwise, this method should return
        [hypervisor_hostname].
        """

        return [self.host_wrapper.mtms.mtms_str]

    def legacy_nwinfo(self):
        """Indicate if the driver requires the legacy network_info format.
        """
        return False

    def get_host_ip_addr(self):
        """Retrieves the IP address of the dom0
        """
        # This code was pulled from the libvirt driver.
        ips = compute_utils.get_machine_ips()
        if CONF.my_ip not in ips:
            LOG.warn(_LW('my_ip address (%(my_ip)s) was not found on '
                         'any of the interfaces: %(ifaces)s'),
                     {'my_ip': CONF.my_ip, 'ifaces': ", ".join(ips)})
        return CONF.my_ip

    def get_volume_connector(self, instance):
        """Get connector information for the instance for attaching to volumes.

        Connector information is a dictionary representing the ip of the
        machine that will be making the connection, the name of the iscsi
        initiator and the hostname of the machine as follows::

            {
                'ip': ip,
                'initiator': initiator,
                'host': hostname
            }

        """
        # The host ID
        connector = {'host': CONF.host}

        # The WWPNs in case of FC connection.
        if self.vol_drvs['fibre_channel'] is not None:
            # Override the host name.
            # TODO(IBM) See if there is a way to support a FC host name that
            # is independent of overall host name.
            connector['host'] = self.vol_drvs['fibre_channel'].host_name(
                self.adapter, self.host_uuid, instance)

            # TODO(IBM) WWPNs should be resolved from instance if previously
            # invoked (ex. Destroy)
            # Set the WWPNs
            wwpn_list = self.vol_drvs['fibre_channel'].wwpns(self.adapter,
                                                             self.host_uuid,
                                                             instance)
            if wwpn_list is not None:
                connector["wwpns"] = wwpn_list
        return connector

    def migrate_disk_and_power_off(self, context, instance, dest,
                                   flavor, network_info,
                                   block_device_info=None,
                                   timeout=0, retry_interval=0):

        disk_info = {}

        # We may be passed a flavor that is in dict format, but the
        # downstream code is expecting an object, so convert it.
        if flavor and not isinstance(flavor, flavor_obj.Flavor):
            flav_obj = flavor_obj.Flavor.get_by_id(context, flavor['id'])
        else:
            flav_obj = flavor

        if flav_obj and flav_obj.root_gb < instance.root_gb:
            raise exception.InstanceFaultRollback(
                exception.ResizeError(reason=_('Cannot reduce disk size.')))

        if dest == self.get_host_ip_addr():
            self._log_operation('resize', instance)
            # This is a local resize
            # Check for disk resizes before VM resources
            if flav_obj.root_gb > instance.root_gb:
                vm.power_off(self.adapter, instance, self.host_uuid)
                # Resize the root disk
                self.disk_dvr.extend_disk(context, instance, dict(type='boot'),
                                          flav_obj.root_gb)

            # Do any VM resource changes
            self._resize_vm(context, instance, flav_obj, retry_interval)
        else:
            self._log_operation('migration', instance)
            raise NotImplementedError()

        # TODO(IBM): The caller is expecting disk info returned
        return disk_info

    def _resize_vm(self, context, instance, flav_obj, retry_interval=0):

        def _delay(attempt, max_attempts, *args, **kwds):
            LOG.info(_LI('Retrying to update VM.'), instance=instance)
            time.sleep(retry_interval)

        @pvm_retry.retry(delay_func=_delay)
        def _update_vm():
            LOG.debug('Resizing instance %s.' % instance.name,
                      instance=instance)
            entry = vm.get_instance_wrapper(self.adapter, instance,
                                            self.host_uuid)

            pwrd = vm.power_off(self.adapter, instance,
                                self.host_uuid, entry=entry)
            # If it was powered off then the etag changed, fetch it again
            if pwrd:
                entry = vm.get_instance_wrapper(self.adapter, instance,
                                                self.host_uuid)

            vm.update(self.adapter, self.host_wrapper,
                      instance, flav_obj, entry=entry)

        # Update the VM
        _update_vm()

    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         block_device_info=None, power_on=True):
        """Completes a resize.

        :param context: the context for the migration/resize
        :param migration: the migrate/resize information
        :param instance: nova.objects.instance.Instance being migrated/resized
        :param disk_info: the newly transferred disk information
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param image_meta: image object returned by nova.image.glance that
                           defines the image from which this instance
                           was created
        :param resize_instance: True if the instance is being resized,
                                False otherwise
        :param block_device_info: instance volume block device info
        :param power_on: True if the instance should be powered on, False
                         otherwise
        """
        # TODO(IBM): Finish this up

        if power_on:
            vm.power_on(self.adapter, instance, self.host_uuid)

    def confirm_migration(self, migration, instance, network_info):
        """Confirms a resize, destroying the source VM.

        :param migration: the migrate/resize information
        :param instance: nova.objects.instance.Instance
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        """
        # TODO(IBM): Anything to do here?
        pass

    def finish_revert_migration(self, context, instance, network_info,
                                block_device_info=None, power_on=True):
        """Finish reverting a resize.

        :param context: the context for the finish_revert_migration
        :param instance: nova.objects.instance.Instance being migrated/resized
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: instance volume block device info
        :param power_on: True if the instance should be powered on, False
                         otherwise
        """
        self._log_operation('revert resize', instance)
        # TODO(IBM): What to do here?  Do we want to recreate the LPAR
        # Or just change the settings back to the flavor?

        # Get the flavor from the instance, so we can revert it
        admin_ctx = ctx.get_admin_context(read_deleted='yes')
        flav_obj = (
            flavor_obj.Flavor.get_by_id(admin_ctx,
                                        instance.instance_type_id))
        # TODO(IBM)  Get the entry once for both power_off and update
        vm.power_off(self.adapter, instance, self.host_uuid)
        vm.update(self.adapter, self.host_uuid, instance, flav_obj)

        if power_on:
            vm.power_on(self.adapter, instance, self.host_uuid)

    def check_can_live_migrate_destination(self, ctxt, instance_ref,
                                           src_compute_info, dst_compute_info,
                                           block_migration=False,
                                           disk_over_commit=False):
        """Validate the destination host is capable of live partition
        migration.

        :param ctxt: security context
        :param instance_ref: instance to be migrated
        :param src_compute_info: source host information
        :param dst_compute_info: destination host information
        :param block_migration: if true, prepare for block migration
        :param disk_over_commit: if true, allow disk over commit
        :returns dest_check_data: dictionary containing destination data
        """
        # dest_check_data = \
        # TODO(IBM): Implement live migration check
        pass

    def check_can_live_migrate_source(self, context, instance,
                                      dest_check_data, block_device_info=None):
        """Check if it is possible to execute live migration.

        This checks if the live migration can succeed, based on the
        results from check_can_live_migrate_destination.

        :param context: security context
        :param instance: nova.db.sqlalchemy.models.Instance
        :param dest_check_data: result of check_can_live_migrate_destination
        :param block_device_info: result of _get_instance_block_device_info
        :returns: a dict containing migration info (hypervisor-dependent)
        """
        # migrate_data = \
        # TODO(IBM): Implement live migration check
        pass

    def pre_live_migration(self, context, instance, block_device_info,
                           network_info, disk_info, migrate_data=None):
        """Prepare an instance for live migration

        :param context: security context
        :param instance: nova.objects.instance.Instance object
        :param block_device_info: instance block device information
        :param network_info: instance network information
        :param disk_info: instance disk information
        :param migrate_data: implementation specific data dict.
        """
        # TODO(IBM): Implement migration prerequisites
        pass

    def live_migration(self, ctxt, instance_ref, dest,
                       post_method, recover_method,
                       block_migration=False, migrate_data=None):
        """Live migrates a partition from one host to another.

        :param ctxt: security context
        :params instance_ref: instance to be migrated.
        :params dest: destination host
        :params post_method: post operation method.
            nova.compute.manager.post_live_migration.
        :params recover_method: recovery method when any exception occurs.
            nova.compute.manager.recover_live_migration.
        :params block_migration: if true, migrate VM disk.
        :params migrate_data: implementation specific data dictionary.
        """
        self._log_operation('live_migration', instance_ref)
        # TODO(IBM): Implement live migration

    def check_instance_shared_storage_local(self, context, instance):
        """Check if instance files located on shared storage.

        This runs check on the destination host, and then calls
        back to the source host to check the results.

        :param context: security context
        :param instance: nova.objects.instance.Instance object
        """
        # Defer to the disk driver method.
        return self.disk_dvr.check_instance_shared_storage_local(
            context, instance)

    def check_instance_shared_storage_remote(self, context, data):
        """Check if instance files located on shared storage.

        :param context: security context
        :param data: result of check_instance_shared_storage_local
        """
        # Defer to the disk driver method.
        return self.disk_dvr.check_instance_shared_storage_remote(
            context, data)

    def check_instance_shared_storage_cleanup(self, context, data):
        """Do cleanup on host after check_instance_shared_storage calls

        :param context: security context
        :param data: result of check_instance_shared_storage_local
        """
        # Defer to the disk driver method.
        return self.disk_dvr.check_instance_shared_storage_cleanup(
            context, data)

    def post_live_migration_at_destination(self, ctxt, instance_ref,
                                           network_info,
                                           block_migration=False,
                                           block_device_info=None):
        """Performs post operations on the destination host
        following a successful live migration.

        :param ctxt: security context
        :param instance_ref: migrated instance
        :param network_info: dictionary of network info for instance
        :param block_migration: boolean for block migration
        """
        # TODO(IBM): Implement post migration
        pass

    @staticmethod
    def _extract_bdm(block_device_info):
        """Returns the block device mapping out of the block device info.

        The block device mapping is a list of dictionaries.  Each dictionary
        represents one volume connection.

        Example:
        [
          {
             'connection_info' {
                'driver_volume_type':'fibre_channel',
                'serial':u'10d9934e-b031-48ff-9f02-2ac533e331c8',
                'data':{
                   'initiator_target_map':{
                      '21000024FF649105':['500507680210E522'],
                      '21000024FF649104':['500507680210E522'],
                      '21000024FF649107':['500507680210E522'],
                      '21000024FF649106':['500507680210E522']
                   },
                   'target_discovered':False,
                   'qos_specs':None,
                   'volume_id':'10d9934e-b031-48ff-9f02-2ac533e331c8',
                   'target_lun':0,
                   'access_mode':'rw',
                   'target_wwn':'500507680210E522'
                }
             },
             'mount_device':'/dev/vda',
             'delete_on_termination':False
          }
       ]
        """
        if block_device_info is None:
            return []
        return block_device_info.get('block_device_mapping', [])

    def get_vnc_console(self, context, instance):
        """Get connection info for a vnc console.

        :param context: security context
        :param instance: nova.objects.instance.Instance

        :returns an instance of console.type.ConsoleVNC
        """
        self._log_operation('get_vnc_console', instance)
        lpar_uuid = vm.get_pvm_uuid(instance)
        port = pvm_vterm.open_vnc_vterm(self.adapter, lpar_uuid,
                                        bind_ip=CONF.vncserver_listen)
        host = CONF.vncserver_proxyclient_address
        return console_type.ConsoleVNC(host=host, port=port)


def _inst_dict(input_dict):
    """Builds a dictionary with instances as values based on the input classes.

    :param input_dict: A dictionary with keys, whose values are class
                       names.
    :returns: A dictionary with the same keys.  But the values are instances
              of the class.  No parameters are passed in  to the init methods.
    """
    response = dict()

    for key in input_dict.keys():
        class_inst = importutils.import_class(input_dict[key])
        response[key] = class_inst()

    return response
