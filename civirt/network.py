#!/usr/bin/env python3

import xml.etree.ElementTree as ET
from pprint import pprint
import libvirt

import logging
import sys


class Network():

    default_uri = 'qemu:///system'

    def connect(self, uri=None):
        '''
        Connect to the libvirt server
        '''
        uri = uri or self.default_uri
        self.conn = libvirt.open(uri)

    def get_info(self, name):

        net = self.conn.networkLookupByName(name)
        xml = str(net.XMLDesc())

        path_domain=".//domain"
        path_net=".//ip"
        path_dhcp=".//dhcp/range"

        root = ET.fromstring(xml)

        gw_ip = None
        gw_mask = None
        dhcp_start = None
        dhcp_end = None


        for net in root.findall(path_net):
            gw_ip = net.attrib.get('address', None)
            gw_mask = net.attrib.get('netmask', None)
            break

        for dhcp in root.findall(path_dhcp):
            dhcp_start = dhcp.attrib.get('start', None)
            dhcp_end = dhcp.attrib.get('end', None)
            break

        for dom in root.findall(path_domain):
            domain = dom.attrib.get('name', None)
            break

        return {
                'domain': domain,
                'dhcp_start': dhcp_start,
                'dhcp_end': dhcp_end,
                'gateway': gw_ip,
                'netmask': gw_mask,
                }



#def main ():
#
#    #dom0 = conn.lookupByName('my-vm-1')
#    #dom0.create()
#
#    pprint (dir(conn))
#
#
#    xml_file = "domain_v1.xml"
#    with open(xml_file) as fp:
#        xml_desc = fp.read()
#
#    #xml_description = '<domain type="kvm"><name>test2_srv</name></domain>'
#    #print (xml_desc)
#    virDomain_obj = conn.defineXML(xml_desc)
#
#
#
#main()
#
