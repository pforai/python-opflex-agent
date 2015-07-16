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

import netaddr
import os
import signal
import sys

from neutron.agent.linux import ip_lib
from neutron.common import config as common_config
from neutron.common import constants as n_constants
from neutron.common import utils as q_utils
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils
from neutron.plugins.openvswitch.agent import ovs_neutron_agent as ovs
from neutron.plugins.openvswitch.common import config  # noqa
from neutron.plugins.openvswitch.common import constants
from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils

from opflexagent import constants as ofcst
from opflexagent import rpc
from opflexagent import snat_iptables_manager

LOG = logging.getLogger(__name__)

gbp_opts = [
    cfg.BoolOpt('hybrid_mode',
                default=False,
                help=_("Whether Neutron's ports can coexist with GBP owned"
                       "ports.")),
    cfg.StrOpt('epg_mapping_dir',
               default='/var/lib/opflex-agent-ovs/endpoints/',
               help=_("Directory where the EPG port mappings will be "
                      "stored.")),
    cfg.ListOpt('opflex_networks',
                default=['*'],
                help=_("List of the physical networks managed by this agent. "
                       "Use * for binding any opflex network to this agent")),
    cfg.ListOpt('internal_floating_ip_pool',
               default=['169.254.0.0/16'],
               help=_("IP pool used for intermediate floating-IPs with SNAT")),
    cfg.ListOpt('internal_floating_ip6_pool',
               default=['fe80::/64'],
               help=_("IPv6 pool used for intermediate floating-IPs "
                      "with SNAT"))
]
cfg.CONF.register_opts(gbp_opts, "OPFLEX")

FILE_EXTENSION = "ep"
FILE_NAME_FORMAT = "%s." + FILE_EXTENSION
METADATA_DEFAULT_IP = '169.254.169.254'


class GBPOvsPluginApi(rpc.GBPServerRpcApiMixin):
    pass


class ExtSegNextHopInfo(object):
    def __init__(self, es_name):
        self.es_name = es_name
        self.ip_start = None
        self.ip_end = None
        self.ip_gateway = None
        self.ip6_start = None
        self.ip6_end = None
        self.ip6_gateway = None
        self.next_hop_iface = None
        self.next_hop_mac = None

    def __str__(self):
        return "%s: ipv4 (%s-%s,%s), ipv6 (%s-%s,%s)" % (self.es_name,
            self.ip_start, self.ip_end, self.ip_gateway,
            self.ip6_start, self.ip6_end, self.ip6_gateway)

    def is_valid(self):
        return ((self.ip_start and self.ip_gateway) or
                (self.ip6_start and self.ip6_gateway))


class GBPOvsAgent(ovs.OVSNeutronAgent):

    def __init__(self, **kwargs):
        self.hybrid_mode = kwargs['hybrid_mode']
        separator = (kwargs['epg_mapping_dir'][-1] if
                     kwargs['epg_mapping_dir'] else '')
        self.epg_mapping_file = (kwargs['epg_mapping_dir'] +
                                 ('/' if separator != '/' else '') +
                                 FILE_NAME_FORMAT)
        self.opflex_networks = kwargs['opflex_networks']
        if self.opflex_networks and self.opflex_networks[0] == '*':
            self.opflex_networks = None
        self.int_fip_pool = {
            4: netaddr.IPSet(kwargs['internal_floating_ip_pool']),
            6: netaddr.IPSet(kwargs['internal_floating_ip6_pool'])}
        if METADATA_DEFAULT_IP in self.int_fip_pool[4]:
            self.int_fip_pool[4].remove(METADATA_DEFAULT_IP)
        self.int_fip_alloc = {4: {}, 6: {}}
        self._load_es_next_hop_info(kwargs['external_segment'])
        self.es_port_dict = {}
        del kwargs['hybrid_mode']
        del kwargs['epg_mapping_dir']
        del kwargs['opflex_networks']
        del kwargs['internal_floating_ip_pool']
        del kwargs['internal_floating_ip6_pool']
        del kwargs['external_segment']

        super(GBPOvsAgent, self).__init__(**kwargs)
        self.supported_pt_network_types = [ofcst.TYPE_OPFLEX]
        self.setup_pt_directory()

    def setup_pt_directory(self):
        directory = os.path.dirname(self.epg_mapping_file)
        if not os.path.exists(directory):
            os.makedirs(directory)
            return
        # Remove all existing EPs mapping
        for f in os.listdir(directory):
            if f.endswith('.' + FILE_EXTENSION):
                try:
                    os.remove(os.path.join(directory, f))
                except OSError as e:
                    LOG.debug(e.message)
        self.snat_iptables.cleanup_snat_all()

    def setup_rpc(self):
        self.agent_state['agent_type'] = ofcst.AGENT_TYPE_OPFLEX_OVS
        self.agent_state['configurations']['opflex_networks'] = (
            self.opflex_networks)
        self.agent_state['binary'] = 'opflex-ovs-agent'
        super(GBPOvsAgent, self).setup_rpc()
        # Set GBP rpc API
        self.of_rpc = GBPOvsPluginApi(rpc.TOPIC_OPFLEX)

    def setup_integration_br(self):
        """Override parent setup integration bridge.

        The opflex agent controls all the flows in the integration bridge,
        therefore we have to make sure the parent doesn't reset them.
        """
        self.int_br.create()
        self.int_br.set_secure_mode()

        self.int_br.delete_port(cfg.CONF.OVS.int_peer_patch_port)
        # The following is executed in the parent method:
        # self.int_br.remove_all_flows()

        if self.hybrid_mode:
            # switch all traffic using L2 learning
            self.int_br.add_flow(priority=1, actions="normal")
        # Add a canary flow to int_br to track OVS restarts
        self.int_br.add_flow(table=constants.CANARY_TABLE, priority=0,
                             actions="drop")
        self.snat_iptables = snat_iptables_manager.SnatIptablesManager(
            self.int_br, self.root_helper)

    def setup_physical_bridges(self, bridge_mappings):
        """Override parent setup physical bridges.

        Only needs to be executed in hybrid mode. If not in hybrid mode, only
        the existence of the integration bridge is assumed.
        """
        self.phys_brs = {}
        self.int_ofports = {}
        self.phys_ofports = {}
        if self.hybrid_mode:
            super(GBPOvsAgent, self).setup_physical_bridges(bridge_mappings)

    def reset_tunnel_br(self, tun_br_name=None):
        """Override parent reset tunnel br.

        Only needs to be executed in hybrid mode. If not in hybrid mode, only
        the existence of the integration bridge is assumed.
        """
        if self.hybrid_mode:
            super(GBPOvsAgent, self).reset_tunnel_br(tun_br_name)

    def setup_tunnel_br(self, tun_br_name=None):
        """Override parent setup tunnel br.

        Only needs to be executed in hybrid mode. If not in hybrid mode, only
        the existence of the integration bridge is assumed.
        """
        if self.hybrid_mode:
            super(GBPOvsAgent, self).setup_tunnel_br(tun_br_name)

    def port_bound(self, port, net_uuid,
                   network_type, physical_network,
                   segmentation_id, fixed_ips, device_owner,
                   ovs_restarted):

        mapping = port.gbp_details
        if not mapping:
            self.mapping_cleanup(port.vif_id)
            if self.hybrid_mode:
                super(GBPOvsAgent, self).port_bound(
                    port, net_uuid, network_type, physical_network,
                    segmentation_id, fixed_ips, device_owner, ovs_restarted)
        elif network_type in self.supported_pt_network_types:
            if ((self.opflex_networks is None) or
                    (physical_network in self.opflex_networks)):
                # Port has to be untagged due to a opflex agent requirement
                self.int_br.clear_db_attribute("Port", port.port_name, "tag")
                self.mapping_to_file(port, mapping, [x['ip_address'] for x in
                                                     fixed_ips], device_owner)
            else:
                # PT cleanup may be needed
                self.mapping_cleanup(port.vif_id)
                LOG.error(_("Cannot provision OPFLEX network for "
                            "net-id=%(net_uuid)s - no bridge for "
                            "physical_network %(physical_network)s"),
                          {'net_uuid': net_uuid,
                           'physical_network': physical_network})
        else:
            LOG.error(_("Network type %(net_type)s not supported for "
                        "Policy Target provisioning. Supported types: "
                        "%(supported)s"),
                      {'net_type': network_type,
                       'supported': self.supported_pt_network_types})

    def port_unbound(self, vif_id, net_uuid=None):
        super(GBPOvsAgent, self).port_unbound(vif_id, net_uuid)
        # Delete epg mapping file
        self.mapping_cleanup(vif_id)

    def mapping_to_file(self, port, mapping, ips, device_owner):
        """Mapping to file.

        Converts the port mapping into file.
        """
<<<<<<< HEAD
        # if device_owner == n_constants.DEVICE_OWNER_DHCP:
        #     ips.append(METADATA_DEFAULT_IP)
=======
        # Skip router-interface ports - they interfere with OVS pipeline
        if device_owner in [n_constants.DEVICE_OWNER_ROUTER_INTF]:
            return
        ips_ext = []
        if device_owner == n_constants.DEVICE_OWNER_DHCP:
            ips_ext.append(METADATA_DEFAULT_IP)
>>>>>>> cd6a05b... Update endpoint file with IP-mapping information
        mapping_dict = {
            "policy-space-name": mapping['ptg_tenant'],
            "endpoint-group-name": (mapping['app_profile_name'] + "|" +
                                    mapping['endpoint_group_name']),
            "interface-name": port.port_name,
            "ip": ips + ips_ext,
            "mac": port.vif_mac,
            "uuid": port.vif_id,
            "promiscuous-mode": mapping['promiscuous_mode']}
        if 'vm-name' in mapping:
            mapping_dict['attributes'] = {'vm-name': mapping['vm-name']}
        self._fill_ip_mapping_info(port.vif_id, mapping, ips, mapping_dict)
        self._write_endpoint_file(port.vif_id, mapping_dict)

    def mapping_cleanup(self, vif_id):
        self._delete_endpoint_file(vif_id)
        es = self._get_es_for_port(vif_id)
        self._dissociate_port_from_es(vif_id, es)
        self._release_int_fip(4, vif_id)
        self._release_int_fip(6, vif_id)

    def treat_devices_added_or_updated(self, devices, ovs_restarted):
        # REVISIT(ivar): This method is copied from parent in order to inject
        # an efficient way to request GBP details. This is needed because today
        # ML2 RPC doesn't allow drivers to add custom information to the device
        # details list.

        skipped_devices = []
        try:
            devices_details_list = self.plugin_rpc.get_devices_details_list(
                self.context,
                devices,
                self.agent_id,
                cfg.CONF.host)
            devices_gbp_details_list = self.of_rpc.get_gbp_details_list(
                self.context, self.agent_id, devices, cfg.CONF.host)
            # Correlate port details
            gbp_details_per_device = {x['device']: x for x in
                                      devices_gbp_details_list if x}
        except Exception as e:
            raise ovs.DeviceListRetrievalError(devices=devices, error=e)
        for details in devices_details_list:
            device = details['device']
            LOG.debug("Processing port: %s", device)
            port = self.int_br.get_vif_port_by_id(device)
            if not port:
                # The port disappeared and cannot be processed
                LOG.info(_("Port %s was not found on the integration bridge "
                           "and will therefore not be processed"), device)
                skipped_devices.append(device)
                continue

            if 'port_id' in details:
                LOG.info(_("Port %(device)s updated. Details: %(details)s"),
                         {'device': device, 'details': details})
                # Inject GBP details
                port.gbp_details = gbp_details_per_device.get(
                    details['device'], {})
                self.treat_vif_port(port, details['port_id'],
                                    details['network_id'],
                                    details['network_type'],
                                    details['physical_network'],
                                    details['segmentation_id'],
                                    details['admin_state_up'],
                                    details['fixed_ips'],
                                    details['device_owner'],
                                    ovs_restarted)
                # update plugin about port status
                # FIXME(salv-orlando): Failures while updating device status
                # must be handled appropriately. Otherwise this might prevent
                # neutron server from sending network-vif-* events to the nova
                # API server, thus possibly preventing instance spawn.
                if details.get('admin_state_up'):
                    LOG.debug(_("Setting status for %s to UP"), device)
                    self.plugin_rpc.update_device_up(
                        self.context, device, self.agent_id, cfg.CONF.host)
                else:
                    LOG.debug(_("Setting status for %s to DOWN"), device)
                    self.plugin_rpc.update_device_down(
                        self.context, device, self.agent_id, cfg.CONF.host)
                LOG.info(_("Configuration for device %s completed."), device)
            else:
                LOG.warn(_("Device %s not defined on plugin"), device)
                if (port and port.ofport != -1):
                    self.port_dead(port)
        return skipped_devices

    def _write_endpoint_file(self, port_id, mapping_dict):
        filename = self.epg_mapping_file % port_id
        if not os.path.exists(os.path.dirname(filename)):
            os.makedirs(os.path.dirname(filename))
        with open(filename, 'w') as f:
            jsonutils.dump(mapping_dict, f)

    def _delete_endpoint_file(self, port_id):
        try:
            os.remove(self.epg_mapping_file % port_id)
        except OSError as e:
            LOG.debug(e.message)

    def _fill_ip_mapping_info(self, port_id, gbp_details, ips, mapping):
        for fip in gbp_details.get('floating_ip', []):
            fm = {'uuid': fip['id'],
                  'mapped-ip': fip['fixed_ip_address'],
                  'floating-ip': fip['floating_ip_address']}
            if 'nat_epg_tenant' in fip:
                fm['policy-space-name'] = fip['nat_epg_tenant']
            if 'nat_epg_name' in fip:
                fm['endpoint-group-name'] = (gbp_details['app_profile_name']
                                             + "|" + fip['nat_epg_name'])
            mapping.setdefault('ip-address-mapping', []).append(fm)

        es_using_int_fip = {4: set(), 6: set()}
        for ipm in gbp_details.get('ip_mapping', []):
            if (not ips or not ipm.get('external_segment_name') or
                not ipm.get('nat_epg_tenant') or
                not ipm.get('nat_epg_name')):
                continue
            es = ipm['external_segment_name']
            epg = gbp_details['app_profile_name'] + "|" + ipm['nat_epg_name']
            ipm['nat_epg_name'] = epg
            next_hop_if, next_hop_mac = self._get_next_hop_info_for_es(ipm)
            if not next_hop_if or not next_hop_mac:
                continue
            for ip in ips:
                ip_ver = netaddr.IPAddress(ip).version
                fip = (self._get_int_fips(ip_ver, port_id).get(es) or
                       self._alloc_int_fip(ip_ver, port_id, es))
                es_using_int_fip[ip_ver].add(es)
                ip_map = {'uuid': uuidutils.generate_uuid(),
                          'mapped-ip': ip,
                          'floating-ip': str(fip),
                          'policy-space-name': ipm['nat_epg_tenant'],
                          'endpoint-group-name': epg,
                          'next-hop-if': next_hop_if,
                          'next-hop-mac': next_hop_mac}
                mapping.setdefault('ip-address-mapping', []).append(ip_map)
        old_es = self._get_es_for_port(port_id)
        new_es = es_using_int_fip[4] | es_using_int_fip[6]
        self._associate_port_with_es(port_id, new_es)
        self._dissociate_port_from_es(port_id, old_es - new_es)

        for ip_ver in es_using_int_fip.keys():
            for es in self._get_int_fips(ip_ver, port_id).keys():
                if es not in es_using_int_fip[ip_ver]:
                    self._release_int_fip(ip_ver, port_id, es)

    def _get_int_fips(self, ip_ver, port_id):
        return self.int_fip_alloc[ip_ver].get(port_id, {})

    def _get_es_for_port(self, port_id):
        """ Return ESs for which there is a internal FIP allocated """
        es = set(self._get_int_fips(4, port_id).keys())
        es.update(self._get_int_fips(6, port_id).keys())
        return es

    def _alloc_int_fip(self, ip_ver, port_id, es):
        fip = self.int_fip_pool[ip_ver].__iter__().next()
        self.int_fip_pool[ip_ver].remove(fip)
        self.int_fip_alloc[ip_ver].setdefault(port_id, {})[es] = fip
        return fip

    def _release_int_fip(self, ip_ver, port_id, es=None):
        if es:
            fips = self.int_fip_alloc[ip_ver].get(port_id, {}).pop(es, None)
            fips = (fips and [fips] or [])
        else:
            fips = self.int_fip_alloc[ip_ver].pop(port_id, {}).values()
        for ip in fips:
            self.int_fip_pool[ip_ver].add(ip)

    def _get_next_hop_info_for_es(self, ipm):
        es_name = ipm['external_segment_name']
        nh = self.ext_seg_next_hop.get(es_name)
        if not nh or not nh.is_valid():
            return (None, None)
        # create ep file for endpoint and snat tables
        if not nh.next_hop_iface:
            try:
                (nh.next_hop_iface, nh.next_hop_mac) = (
                    self.snat_iptables.setup_snat_for_es(es_name,
                        nh.ip_start, nh.ip_end, nh.ip_gateway,
                        nh.ip6_start, nh.ip6_end, nh.ip6_gateway))
            except Exception as e:
                LOG.error(_("Error while creating SNAT iptables for "
                            "%{es}s: %{ex}s"),
                          {'es': es_name, 'ex': e})
            self._create_host_endpoint_file(ipm, nh)
        return (nh.next_hop_iface, nh.next_hop_mac)

    def _create_host_endpoint_file(self, ipm, nh):
        ips = []
        for s, e in [(nh.ip_start, nh.ip_end), (nh.ip6_start, nh.ip6_end)]:
            if s:
                ips.extend(list(netaddr.iter_iprange(s, e or s)))
        ep_dict = {
            "policy-space-name": ipm['nat_epg_tenant'],
            "endpoint-group-name": ipm['nat_epg_name'],
            "interface-name": nh.next_hop_iface,
            "ip": [str(x) for x in ips],
            "mac": nh.next_hop_mac,
            "uuid": uuidutils.generate_uuid(),
            "promiscuous-mode": True}
        self._write_endpoint_file(nh.es_name, ep_dict)

    def _associate_port_with_es(self, port_id, ess):
        for es in ess:
            self.es_port_dict.setdefault(es, set()).add(port_id)

    def _dissociate_port_from_es(self, port_id, ess):
        for es in ess:
            if es not in self.es_port_dict:
                continue
            self.es_port_dict[es].discard(port_id)
            if self.es_port_dict[es]:
                continue
            self.es_port_dict.pop(es)
            if es in self.ext_seg_next_hop:
                self.ext_seg_next_hop[es].next_hop_iface = None
                self.ext_seg_next_hop[es].next_hop_mac = None
            self._delete_endpoint_file(es)
            try:
                self.snat_iptables.cleanup_snat_for_es(es)
            except Exception as e:
                LOG.warn(_("Failed to remove SNAT iptables for "
                           "%{es}s: %{ex}s"),
                         {'es': es, 'ex': e})

    def _load_es_next_hop_info(self, es_cfg):
        def parse_range(val):
            if val and val[0]:
                ip = [x.strip() for x in val[0].split(',', 1)]
                return (ip[0] or None,
                        (len(ip) > 1 and ip[1]) and ip[1] or None)
            return (None, None)

        def parse_gateway(val):
            return (val and '/' in val[0]) and val[0] or None

        self.ext_seg_next_hop = {}
        for es_name, es_info in es_cfg.iteritems():
            nh = ExtSegNextHopInfo(es_name)
            for key, value in es_info:
                if key == 'ip_address_range':
                    (nh.ip_start, nh.ip_end) = parse_range(value)
                elif key == 'ip_gateway':
                    nh.ip_gateway = parse_gateway(value)
                elif key == 'ip6_address_range':
                    (nh.ip6_start, nh.ip6_end) = parse_range(value)
                elif key == 'ip6_gateway':
                    nh.ip6_gateway = parse_gateway(value)
            self.ext_seg_next_hop[es_name] = nh
            LOG.debug(_("Found external segment: %s") % nh)


def create_agent_config_map(conf):
    agent_config = ovs.create_agent_config_map(conf)
    agent_config['hybrid_mode'] = conf.OPFLEX.hybrid_mode
    agent_config['epg_mapping_dir'] = conf.OPFLEX.epg_mapping_dir
    agent_config['opflex_networks'] = conf.OPFLEX.opflex_networks
    agent_config['internal_floating_ip_pool'] = (
        conf.OPFLEX.internal_floating_ip_pool)
    agent_config['internal_floating_ip6_pool'] = (
        conf.OPFLEX.internal_floating_ip6_pool)
    # DVR not supported
    agent_config['enable_distributed_routing'] = False
    # ARP responder not supported
    agent_config['arp_responder'] = False

    # read external-segment next-hop info
    es_info = {}
    multi_parser = cfg.MultiConfigParser()
    multi_parser.read(conf.config_file)
    for parsed_file in multi_parser.parsed:
        for parsed_item in parsed_file.keys():
            if parsed_item.startswith('opflex_external_segment:'):
                es_name = parsed_item.split(':', 1)[1]
                if es_name:
                    es_info[es_name] = parsed_file[parsed_item].items()
    agent_config['external_segment'] = es_info
    return agent_config


def main():
    cfg.CONF.register_opts(ip_lib.OPTS)
    common_config.init(sys.argv[1:])
    common_config.setup_logging()
    q_utils.log_opt_values(LOG)

    try:
        agent_config = create_agent_config_map(cfg.CONF)
    except ValueError as e:
        LOG.error(_('%s Agent terminated!'), e)
        sys.exit(1)

    is_xen_compute_host = 'rootwrap-xen-dom0' in agent_config['root_helper']
    if is_xen_compute_host:
        # Force ip_lib to always use the root helper to ensure that ip
        # commands target xen dom0 rather than domU.
        cfg.CONF.set_default('ip_lib_force_root', True)
    agent = GBPOvsAgent(**agent_config)
    signal.signal(signal.SIGTERM, agent._handle_sigterm)

    # Start everything.
    LOG.info(_("Agent initialized successfully, now running... "))
    agent.daemon_loop()


if __name__ == "__main__":
    main()
<<<<<<< HEAD

=======
>>>>>>> cd6a05b... Update endpoint file with IP-mapping information
