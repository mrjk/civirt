import os
import re
import shlex
import logging
import subprocess
from io import BytesIO
import pycdlib
import yaml
from civirt.exceptions import *
from civirt.libvirt import Network, Instance

from dicttoxml import dicttoxml
import xml.etree.ElementTree as ET

LOGGER = logging.getLogger(__name__)
HOSTSFILE = "/etc/hosts"
HOSTS_ENTRY_SUFFIX = "# added by civirt"

class VirtualMachine:
    def __init__(self, settings):

        # Get settings
        self.hostname = settings['hostname']
        self.network = settings.get('network', 'default')
        self.domain = settings.get('domain', '.local')

        self.variant = settings['variant']
        self.cpu = settings.get('cpu', 1)
        self.mem = settings.get('mem', 512)

        self.volumes = settings.get('volumes', [])
        self.ssh_keys = settings.get('ssh_keys', [])
        self.userdata = settings.get('userdata', None)

        # Generated data
        self.directory = settings['directory']
        self.name='_'.join([self.network, self.hostname])

        # Fetch domain from libvirt network
        self.get_net()
        self.fqdn = '.'.join([self.hostname, self.domain])

        # Copy cloudinit settings to a dedicated dict.
        #self.cloudinit = {'metadata': settings['metadata'],
        #                  'userdata': settings['userdata']}

        # Save the fully qualified paths for qcow2/iso files in self.disks
        self.qcow2 = {}
        self.qcow2['bdisk'] =  settings['backingdisk']
        self.qcow2['size'] = settings['size']
        self.qcow2['path'] = os.path.join(settings['directory'], f"{self.name}.qcow2")

        # Cloudinit: Resolvers
        userdata = settings['userdata']
        userdata_resolve = {}
        userdata_nameservers = settings.get('nameservers', None)
        if userdata_nameservers:
            userdata_resolve['nameservers'] = userdata_nameservers
        if self.domain:
            userdata_resolve['domain'] = self.domain
            userdata_resolve['searchdomains'] = [self.domain]

        # Cloudinit: Main config
        self.userdata['manage_resolv_conf'] = True
        self.userdata['resolv_conf'] = userdata_resolve
        self.userdata['hostname'] = self.hostname
        self.userdata['fqdn'] = self.fqdn
        self.userdata['ssh_authorized_keys'] = self.ssh_keys

        # Create metadata
        self.metadata = {
            'instance_id': self.name,
            'local-hostname': self.hostname,
                }

        # Save cloudinit config
        self.cloudinit={}
        self.cloudinit['metadata'] = self.metadata
        self.cloudinit['userdata'] = self.userdata
        self.cloudinit['path'] = os.path.join(settings['directory'], f"{self.name}.iso")




    def __repr__(self):
        return yaml.dump(self.__dict__, default_flow_style=False)


    def create(self):
        '''
        Provision the virtual machine
        '''
        # Update disk settings
        if not os.path.isdir(self.directory):
            os.makedirs(self.directory)


        # Query libvirt API
        if self.is_instance_defined():
            LOGGER.info(f"{self.name} - Instance is already up and running.")
            return

        # Create backing disk
        self.create_disk()
        # Generate xml with virt-install
        self.create_vm()
        # Ready network config that is to be written to NoCloud Iso
        self.generate_netdata()
        # Create nocloud iso
        self.create_iso()
        # Attach the iso file
        self.attach_iso()
        # Start the VM
        self.start_vm()
        # Save provisionning metada in vm
        self.metadata_vm()


    @staticmethod
    def domain_is_defined(domain):
        cmd = ['virsh', '--connect', 'qemu:///system', 'dumpxml', domain]
        return subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode

    def delete(self):
        '''
        Delete the virtual machine
        '''
        # Libvirt cleanup
        if not VirtualMachine.domain_is_defined(self.name):
            self.cleanup_libvirt()
        else:
            LOGGER.info(f"{self.name} - Libvirt needs no cleanup.")

        # Remove qcow2 disk
        if os.path.isfile(self.qcow2['path']):
            self.delete_file(self.qcow2['path'])
        else:
            LOGGER.info(f"{self.name} - Qcow2 disk does not exist.")

        # Remove nocloud ISO
        if os.path.isfile(self.cloudinit['path']):
            self.delete_file(self.cloudinit['path'])
        else:
            LOGGER.info(f"{self.name} - Cloudinit iso does not exist.")

        # Remove volumes without names
        vol_incr = 1
        for vol in self.volumes:
            vol_name = vol.get('name', None)
            if vol_name is None:
                vol_dir = vol.get('dir', self.directory)
                vol_path = f"{vol_dir}/{self.name}_disk{vol_incr}.qcow2"

                if os.path.isfile(vol_path):
                    self.delete_file(vol_path)
                else:
                    LOGGER.info(f"{self.name} - Instance disk {vol_path} does not exists.")
                vol_incr = vol_incr + 1

        # Remove the output directory
        if os.listdir(self.directory) is None:
            os.rmdir(self.directory)
        LOGGER.info(f"{self.name} - Successfully deleted. ")

    def create_disk(self):
        '''
        create a qcow2 disk for the virtual machine.
        '''
        if not os.path.isfile(self.qcow2['bdisk']):
            raise BackingDiskException(f"{self.name} - Backing disk at "
                                       f"'{self.qcow2['bdisk']}' does not exist.")

        if os.path.isfile(self.qcow2['path']):
            LOGGER.info(f"{self.name} - Disk qcow2 already exists in "
                        f"'{self.qcow2['path']}'.")
            return


        cmd = ['qemu-img', 'create', '-b', self.qcow2['bdisk'], '-f', 'qcow2',
               '-F', 'qcow2', self.qcow2['path']]
        # Append the new disk's size to the qemu-img, if configured.
        if self.qcow2['size']:
            cmd.append(str(self.qcow2['size']))
        try:
            subprocess.check_call(cmd, stderr=subprocess.STDOUT)
            LOGGER.info(f"{self.name} - Created qcow2 disk at "
                        f"'{self.qcow2['path']}'.")
        except subprocess.CalledProcessError as err:
            LOGGER.critical(f"{self.name} : Exception creating qcow2 disk at "
                            f"'{self.qcow2['path']}'."
                            f"Command output: {str(err.output)}")
            raise

    def get_net(self):
        '''
        Retrieve network informations from libvirt network
        '''

        net = Network()
        net.connect()
        net_infos = net.get_info(self.network)

        if net_infos.get('domain', None) is None:
            raise Exception(f"ERROR: No domain name configured for network {self.network}")

        self.domain = net_infos.get('domain', None) or self.net['domain']


    def is_instance_defined(self):
        '''
        Retrieve instance informations from libvirt instance
        '''

        inst = Instance()
        inst.connect()

        try:
            inst_infos = inst.get_info(self.name )
            return True
        except:
            return False



    def create_vm(self):
        '''
        create libvirt/kvm domain using virt-install
        '''
        fqdn = self.name
        cmd = ['virt-install', '--connect', 'qemu:///system', '--import', f'--os-variant={self.variant}',
               '--autostart',
               '--metadata', f"title={self.fqdn}",
               '--noautoconsole', '--network', f'network={self.network},model=virtio',
               '--vcpus', str(self.cpu), '--ram', str(self.mem),
               '--print-xml']

        cmd.extend(['--name', self.name])
        cmd.extend(['--disk', os.path.abspath(self.qcow2['path'])+',format=qcow2,bus=virtio'])
        cmd.extend(['--check', 'disk_size=off'])

        # Check if the vm dos not aleready exists

        # Append extra volumes
        vol_prefix = 'vol_'
        vol_incr = 1
        for vol in self.volumes:
            vol_name = vol.get('name', None)
            vol_size = vol.get('size', '40')
            vol_dir = vol.get('dir', self.directory)

            if not vol_name:
                vol_name = f"{self.name}_disk{vol_incr}"
                vol_incr = vol_incr + 1
            else:
                vol_name = f"{vol_prefix}{vol_name}"

            vol_cmd = [ '--disk',
                    f"{vol_dir}/{vol_name}.qcow2,format=qcow2,bus=virtio,size={vol_size}"
                    ]
            cmd.extend(vol_cmd)

        LOGGER.debug("Exec: " + ' '.join(cmd))
        try:
            # generate the xml configuration
            self.domainxml = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
            #self.domainxml.decode('utf-8')
        except subprocess.CalledProcessError as err:
            LOGGER.critical(f'{fqdn} - Failure generating libvirt xml. '
                            f'Cmd output: {str(err.output)}')
            raise

        # create the virtual machine
        cmd_to_define_vm = subprocess.Popen(["virsh", '--connect', 'qemu:///system', "define", "/dev/stdin"],
                                            stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT)
        cmd_to_define_vm.stdin.write(self.domainxml)
        cmd_to_define_vm.communicate()
        cmd_to_define_vm.wait()
        if cmd_to_define_vm.returncode != 0:
            LOGGER.critical(f'{fqdn} - Exception creating virtual machine '
                            f'using virsh.')
            raise subprocess.CalledProcessError
        else:
            LOGGER.info(f'{fqdn} - Successfully defined virtual machine'
                        f' using virsh.')


    def generate_netdata(self):
        '''
        Builds NoCloud network config for the given IP
        '''
        # Code to pull in mac address
        pattern = re.compile('<mac address=(.*)/>')
        try:
            macaddr = pattern.search(self.domainxml.decode('utf-8')).groups()[0]
        except AttributeError:
            LOGGER.critical('%s : no mac address found in vm\'s xml. '
                            'This will result in broken network config.')
            raise NoMacAddressException

        #self.cloudinit['netdata'] = {
        #    'version': 2,
        #    'ethernets': {
        #        'interface0': {
        #            'match': {'macaddress': macaddr.strip('\"')},
        #            'set-name': 'eth0',
        #            'addresses': [str(self.domain['ipaddr'])+'/24'],
        #            'gateway4': '192.168.122.1',
        #            'nameservers' : {'addresses': ['192.168.122.1']}
        #            }
        #     }
        #}
        self.cloudinit['netdata'] = {
            'version': 2,
            'ethernets': {
                'eno1': {
                    'dhcp4': True,
                    }
             }
        }

    def create_iso(self):
        '''
        create a cloud-init iso from {user/meta}data dictionaries.
        '''

        # Create an ISO
        iso = pycdlib.PyCdlib()
        # Set label to "cidata"
        iso.new(interchange_level=3,
                joliet=True,
                sys_ident='LINUX',
                vol_ident='cidata'
               )
        metadata = yaml.dump(self.cloudinit['metadata'], default_style="\\")
        userdata = "#cloud-config\n" + yaml.dump(self.cloudinit['userdata'], default_style="\\")
        netdata = yaml.dump(self.cloudinit['netdata'], default_style="\\")

        # Calculate sizes of the files to write.
        msize = len(metadata)
        usize = len(userdata)
        nwsize = len(netdata)

        # Add files to iso
        iso.add_fp(BytesIO(f"{userdata}".encode()), usize, '/USERDATA.;1', joliet_path='/user-data')
        iso.add_fp(BytesIO(f"{metadata}".encode()), msize, '/METADATA.;1', joliet_path='/meta-data')
        iso.add_fp(BytesIO(f"{netdata}".encode()), nwsize, '/NETWORKCONFIG.;1', joliet_path='/network-config')

        try:
            # Write the iso file
            iso.write(self.cloudinit['path'])
            LOGGER.info(f"{self.name} - Created nocloud iso at "
                        f"'{self.cloudinit['path']}'.")
        except IOError:
            LOGGER.critical(f"{self.name} - Failure creating the "
                            f"nocloud iso at '{self.cloudinit['path']}'.")
            raise


    def attach_iso(self):
        '''
        Attach created iso to the virtual machine
        '''
        cmd = ['virsh', '--connect', 'qemu:///system', 'attach-disk', '--persistent', self.name,
               self.cloudinit['path'], 'vdz', '--type', 'cdrom' ]
        try:
            subprocess.check_output(cmd, stderr=subprocess.STDOUT)
            LOGGER.info(f"{self.name} - Attached nocloud iso to the vm.")
        except subprocess.CalledProcessError as err:
            LOGGER.critical(f"{self.name} - Failure attaching iso. Cmd output:"
                            f"{str(err.output)}")
            raise


    def start_vm(self):
        '''
        Start the virtual machine
        '''
        cmd = ['virsh', '--connect', 'qemu:///system', 'start', self.name]
        try:
            subprocess.check_call(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT)
            LOGGER.info(f"{self.name} - Successfully started.")
        except subprocess.CalledProcessError as err:
            LOGGER.critical(f"{self.name} - Failure starting virtual "
                            f"machine. Cmd output: {str(err.output)}")
            raise




    def delete_file(self, filepath):
        '''
        delete file on disk.
        :param filepath: file to delete
        :type filepath: str
        '''
        try:
            os.remove(filepath)
            LOGGER.info(f"{self.name} -  Removed {filepath}")
        except IOError as err:
            LOGGER.critical(f"{self.name} - Exception removing "
                            f"{filepath}. {err}")
            raise

    def metadata_vm(self):

        # Rework vm_meta
        vm_meta = dict(self.__dict__)
        vm_meta.pop('cloudinit', None)
        vm_meta.pop('domainxml', None)
        vm_meta.pop('userdata', None)
        #vm_meta = {f'civirt_{k}': v for k, v in vm_meta.items()}
        vm_meta['ansible_host'] = self.fqdn
        vm_meta['ansible_user'] = 'sysmaint'

        # Disable logging
        loggin_dicttoxml = logging.getLogger('dicttoxml')
        loggin_dicttoxml.setLevel(logging.WARNING)
        URI = "https://github.com/mrjk/civirt/1.0"

        # Ugly trick to remove root key
        xml = dicttoxml(vm_meta)
        xml = ET.fromstring(xml)
        xml = ET.tostring(xml).decode("utf-8")

        LOGGER.info(f"{self.name} - Update instance metadata.")
        cmd = [
                "virsh", "--connect", "qemu:///system", "metadata", self.name, URI,
                "--key", 'civirt',
                '--set', xml,
                '--config', '--live']
        LOGGER.debug("Exec: " + ' '.join(cmd))
        try:
            self.domainxml = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as err:
            LOGGER.warning(f'{self.name} - Failure saving libvirt xml metadata. '
                            f'Cmd output: {str(err.output)}')


    def cleanup_libvirt(self):
        '''
        stop and cleanup virtual machine config from libvirt.
        '''
        stopcmd = f"virsh --connect qemu:///system destroy {self.name}"
        delcmd = f"virsh --connect qemu:///system undefine {self.name}"

        try:
            subprocess.call(shlex.split(stopcmd), stderr=subprocess.PIPE, stdout=subprocess.PIPE)
            subprocess.check_output(shlex.split(delcmd), stderr=subprocess.STDOUT)
            LOGGER.info(f"{self.name} - Stopped and undefined in "
                        f"libvirt")
        except subprocess.CalledProcessError as err:
            err_output = str(err.output.rstrip('\n'))
            LOGGER.critical(f"{self.name} - Failure stopping or undefining "
                            f"virtual machine in libvirt. Cmd output: "
                            f"{err_output}")
