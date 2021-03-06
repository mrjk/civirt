#!/usr/bin/env python3
import logging
import sys
import string
import random
import copy
import yaml
from civirt.virtualmachine import VirtualMachine

LOGGER = logging.getLogger() #TODO: Using the root logger sets loglevels everywhere
logformat = logging.Formatter('%(asctime)s - %(funcName)s - '
                              '%(levelname)s - %(message)s')
logformat = logging.Formatter( '%(levelname)s - %(message)s')
stdouthandler = logging.StreamHandler(sys.stdout)
stdouthandler.setFormatter(logformat)
LOGGER.addHandler(stdouthandler)
LOGGER.setLevel(logging.INFO)

def _prepareconfig(file, included=False):
    '''
    Takes in the project's config.yaml and returns proper python dicts with
    which VirtualMachine objects can be instantiated.
    '''
    compiledconfig = {}
    try:
        with open(file, 'r') as reader:
            config = yaml.safe_load(reader)
    except (IOError, yaml.YAMLError) as err:
        LOGGER.critical(f'Exception reading/parsing configuration file. '
                        f'Will exit. {str(err)}')
        raise

    # Import the top-scoped settings 'common' to all VMs.
    common_settings = config.get('common', {})

    # Return only common field if it is an included config
    if included == True:
        return common_settings

    # Import other files if any
    import_config = config.get('import_common', None)
    if import_config:
        imported_settings = _prepareconfig(import_config, included=True)
        common_settings = {**imported_settings, **common_settings}

    # Loop over each VMs
    for vm in config['vms']:
        try:
            vm_name = vm.get('hostname')

            # Create a deepcopy otherwise both keys end up using the same dict object
            vm_settings = copy.deepcopy(common_settings)
            # Put VM specific settings overridding and merging with 'common'
            vm_settings.update(vm)
            # Create and add cloud-init's metadata file
            #vm_settings['metadata'] = {}
            #vm_settings['metadata']['instance_id'] = (vm_name +
            #    ''.join(random.choice(string.ascii_uppercase + string.digits)
            #            for _ in range(5)))
            #vm_settings['metadata']['local-hostname'] = vm.hostname

            LOGGER.debug(f"{vm_name} -- Configuration ready.")
        except Exception as err:
            LOGGER.exception(f"{vm_name} -- Exception generating config."
                             f"{err}")
            raise
        compiledconfig.update({vm_name: vm_settings})
    return compiledconfig


def executor(cfgfile, action):
    '''
    Create virtual machines.
    '''
    conf = _prepareconfig(cfgfile)
    for fqdn, vm_settings in conf.items():
        try:
            #print(fqdn, vm_settings)
            vm = VirtualMachine(vm_settings)
            #print(vm)
            eval(f'vm.{action}()')
            LOGGER.info(f'{vm.name} - Operation {action} successful.')
        except Exception as err:
            LOGGER.exception(f'{vm.name} -- Operation {action} failed. '
                             f'Err: {err}')

def create(cfgfile):
    executor(cfgfile, 'create')

def delete(cfgfile):
    executor(cfgfile, 'delete')
