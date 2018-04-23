# Copyright (c) 2013 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from oslo_config import cfg
from oslo_log import log as logging
from sqlalchemy import and_, or_
from sqlalchemy.orm import Query

from neutron_lib.api.definitions import portbindings
from neutron_lib import constants as n_const
from neutron_lib import context as nctx
from neutron_lib.plugins.ml2 import api as driver_api

import neutron.db.api as db
from neutron.db import db_base_plugin_v2
from neutron.db.models import segment as segment_models
from neutron.db import models_v2
from neutron.db import securitygroups_db as sec_db
from neutron.db import segments_db
from neutron.plugins.ml2 import models as ml2_models

from networking_arista.common import db as anet_models
from networking_arista.common import utils

LOG = logging.getLogger(__name__)


def join_if_necessary(query, *args, **kwargs):
    table = args[0]
    if table in [t.entity for t in query._join_entities]:
        return query
    elif table in query._primary_entity.entities:
        return query
    return query.join(*args, **kwargs)


def outerjoin_if_necessary(query, *args, **kwargs):
    table = args[0]
    if table in [t.entity for t in query._join_entities]:
        return query
    elif table in query._primary_entity.entities:
        return query
    return query.outerjoin(*args, **kwargs)


def filter_network_type(query):
    """Filter unsupported segment types"""
    segment_model = segment_models.NetworkSegment
    query = (query
             .filter(
                 segment_model.network_type.in_(
                     utils.SUPPORTED_NETWORK_TYPES)))
    return query


def filter_unbound_ports(query):
    """Filter ports not bound to a host or network"""
    # hack for pep8 E711: comparison to None should be
    # 'if cond is not None'
    none = None
    port_model = models_v2.Port
    binding_level_model = ml2_models.PortBindingLevel
    query = (query
             .join_if_necessary(port_model)
             .join_if_necessary(binding_level_model)
             .filter(
                 binding_level_model.host != '',
                 port_model.device_id != none,
                 port_model.network_id != none))
    return query


def filter_by_device_owner(query, device_owners=None):
    """Filter ports by device_owner

    Either filter using specified device_owner or using the list of all
    device_owners supported and unsupported by the arista ML2 plugin
    """
    port_model = models_v2.Port
    if not device_owners:
        device_owners = utils.SUPPORTED_DEVICE_OWNERS
    supported_device_owner_filter = [
        port_model.device_owner.ilike('%s%%' % owner)
        for owner in device_owners]
    unsupported_device_owner_filter = [
        port_model.device_owner.notilike('%s%%' % owner)
        for owner in utils.UNSUPPORTED_DEVICE_OWNERS]
    query = (query
             .filter(
                 and_(*unsupported_device_owner_filter),
                 or_(*supported_device_owner_filter)))
    return query


def filter_by_device_id(query):
    """Filter ports attached to devices we don't care about

    Currently used to filter DHCP_RESERVED ports
    """
    port_model = models_v2.Port
    unsupported_device_id_filter = [
        port_model.device_id.notilike('%s%%' % id)
        for id in utils.UNSUPPORTED_DEVICE_IDS]
    query = (query
             .filter(and_(*unsupported_device_id_filter)))
    return query


def filter_by_vnic_type(query, vnic_type):
    """Filter ports by vnic_type (currently only used for baremetals)"""
    port_model = models_v2.Port
    binding_model = ml2_models.PortBinding
    dst_binding_model = ml2_models.DistributedPortBinding
    query = (query
             .outerjoin_if_necessary(
                 binding_model,
                 port_model.id == binding_model.port_id)
             .outerjoin_if_necessary(
                 dst_binding_model,
                 port_model.id == dst_binding_model.port_id)
             .filter(
                 (binding_model.vnic_type == vnic_type) |
                 (dst_binding_model.vnic_type == vnic_type)))
    return query


def filter_unmanaged_physnets(query):
    """Filter ports managed by other ML2 plugins """
    config = cfg.CONF.ml2_arista
    managed_physnets = config['managed_physnets']

    # Filter out ports bound to segments on physnets that we're not
    # managing
    segment_model = segment_models.NetworkSegment
    if managed_physnets:
        query = (query
                 .join_if_necessary(segment_model)
                 .filter(segment_model.physical_network.in_(
                     managed_physnets)))
    return query


def filter_inactive_ports(query):
    """Filter ports that aren't in active status """
    port_model = models_v2.Port
    query = (query
             .filter(port_model.status == n_const.PORT_STATUS_ACTIVE))
    return query


def filter_unnecessary_ports(query, device_owners=None, vnic_type=None,
                             active=True):
    """Filter out all ports are not needed on CVX """
    query = (query
             .filter_unbound_ports()
             .filter_by_device_owner(device_owners)
             .filter_by_device_id()
             .filter_unmanaged_physnets())
    if active:
        query = query.filter_inactive_ports()
    if vnic_type:
        query = query.filter_by_vnic_type(vnic_type)
    return query


Query.join_if_necessary = join_if_necessary
Query.outerjoin_if_necessary = outerjoin_if_necessary
Query.filter_network_type = filter_network_type
Query.filter_unbound_ports = filter_unbound_ports
Query.filter_by_device_owner = filter_by_device_owner
Query.filter_by_device_id = filter_by_device_id
Query.filter_by_vnic_type = filter_by_vnic_type
Query.filter_unmanaged_physnets = filter_unmanaged_physnets
Query.filter_inactive_ports = filter_inactive_ports
Query.filter_unnecessary_ports = filter_unnecessary_ports


@staticmethod
def get_tenants(tenant_id=None):
    """Returns list of all project/tenant ids that may be relevant on CVX"""
    session = db.get_reader_session()
    project_ids = set()
    with session.begin():
        network_model = models_v2.Network
        project_ids |= set(pid[0] for pid in
                           session.query(network_model.project_id).distinct())
        port_model = models_v2.Port
        project_ids |= set(pid[0] for pid in
                           session.query(port_model.project_id).distinct())
        if tenant_id:
            project_ids &= set([tenant_id])
    return [{'project_id': project_id} for project_id in project_ids]


@staticmethod
def get_networks(network_id=None):
    """Returns list of all networks that may be relevant on CVX"""
    session = db.get_reader_session()
    with session.begin():
        model = models_v2.Network
        networks = session.query(model)
        if network_id:
            networks = networks.filter(model.id == network_id)
    return networks.all()


@staticmethod
def get_segments(segment_id=None):
    """Returns list of all network segments that may be relevant on CVX"""
    session = db.get_reader_session()
    with session.begin():
        model = segment_models.NetworkSegment
        segments = session.query(model).filter_network_type()
        if segment_id:
            segments = segments.filter(model.id == segment_id)
    return segments.all()


def get_instances(device_owners=None, vnic_type=None, instance_id=None):
    """Returns filtered list of all instances in the neutron db"""
    session = db.get_reader_session()
    with session.begin():
        port_model = models_v2.Port
        binding_model = ml2_models.PortBinding
        instances = (session
                     .query(port_model,
                            binding_model)
                     .outerjoin(
                         binding_model,
                         port_model.id == binding_model.port_id)
                     .distinct(port_model.device_id)
                     .group_by(port_model.device_id)
                     .filter_unnecessary_ports(device_owners, vnic_type))
        if instance_id:
            instances = instances.filter(port_model.device_id == instance_id)
    return instances.all()


@staticmethod
def get_dhcp_instances(instance_id=None):
    """Returns filtered list of DHCP instances that may be relevant on CVX"""
    return get_instances(device_owners=[n_const.DEVICE_OWNER_DHCP],
                         instance_id=instance_id)


@staticmethod
def get_router_instances(instance_id=None):
    """Returns filtered list of routers that may be relevant on CVX"""
    return get_instances(device_owners=[n_const.DEVICE_OWNER_DVR_INTERFACE],
                         instance_id=instance_id)


@staticmethod
def get_vm_instances(instance_id=None):
    """Returns filtered list of vms that may be relevant on CVX"""
    return get_instances(device_owners=[n_const.DEVICE_OWNER_COMPUTE_PREFIX],
                         vnic_type=portbindings.VNIC_NORMAL,
                         instance_id=instance_id)


@staticmethod
def get_baremetal_instances(instance_id=None):
    """Returns filtered list of baremetals that may be relevant on CVX"""
    return get_instances(vnic_type=portbindings.VNIC_BAREMETAL)


def get_ports(device_owners=None, vnic_type=None, port_id=None, active=True):
    """Returns list of all ports in neutron the db"""
    session = db.get_reader_session()
    with session.begin():
        port_model = models_v2.Port
        ports = (session
                 .query(port_model)
                 .filter_unnecessary_ports(device_owners, vnic_type, active))
        if port_id:
            ports = ports.filter(port_model.id == port_id)
    return ports.all()


@staticmethod
def get_dhcp_ports(port_id=None):
    """Returns filtered list of DHCP instances that may be relevant on CVX"""
    return get_ports(device_owners=[n_const.DEVICE_OWNER_DHCP],
                     port_id=port_id)


@staticmethod
def get_router_ports(port_id=None):
    """Returns filtered list of routers that may be relevant on CVX"""
    return get_ports(device_owners=[n_const.DEVICE_OWNER_DVR_INTERFACE],
                     port_id=port_id)


@staticmethod
def get_vm_ports(port_id=None):
    """Returns filtered list of vms that may be relevant on CVX"""
    return get_ports(device_owners=[n_const.DEVICE_OWNER_COMPUTE_PREFIX],
                     vnic_type=portbindings.VNIC_NORMAL, port_id=port_id)


@staticmethod
def get_baremetal_ports(port_id=None):
    """Returns filtered list of baremetals that may be relevant on CVX"""
    return get_ports(vnic_type=portbindings.VNIC_BAREMETAL,
                     port_id=port_id)


@staticmethod
def get_port_bindings(binding_key=None):
    """Returns filtered list of port bindings that may be relevant on CVX"""
    session = db.get_reader_session()
    with session.begin():
        binding_level_model = ml2_models.PortBindingLevel
        port_binding_model = anet_models.PortBindingWithLevels
        dist_binding_model = anet_models.DistributedPortBindingWithLevels
        bindings = (session.query(port_binding_model)
                    .outerjoin(
                        binding_level_model,
                        and_(port_binding_model.port_id ==
                             binding_level_model.port_id,
                             port_binding_model.host ==
                             binding_level_model.host))
                    .filter_unnecessary_ports())
        dist_bindings = (session.query(dist_binding_model)
                         .outerjoin(
                             binding_level_model,
                             and_(dist_binding_model.port_id ==
                                  binding_level_model.port_id,
                                  dist_binding_model.host ==
                                  binding_level_model.host))
                         .filter_unnecessary_ports())
        if binding_key:
            port_id = binding_key[0]
            if type(binding_key[1]) == tuple:
                switch_id = binding_key[1][0]
                switch_port = binding_key[1][1]
                bindings = bindings.filter(and_(
                    port_binding_model.port_id == port_id,
                    port_binding_model.profile.ilike('%%%s%%' % switch_id),
                    port_binding_model.profile.ilike('%%%s%%' % switch_port)))
                dist_bindings = dist_bindings.filter(and_(
                    dist_binding_model.port_id == port_id,
                    dist_binding_model.profile.ilike('%%%s%%' % switch_id),
                    dist_binding_model.profile.ilike('%%%s%%' % switch_port)))
            else:
                host_id = binding_key[1]
                bindings = bindings.filter(and_(
                    port_binding_model.port_id == port_id,
                    port_binding_model.host == host_id))
                dist_bindings = dist_bindings.filter(and_(
                    dist_binding_model.port_id == port_id,
                    dist_binding_model.host == host_id))
    return bindings.all() + dist_bindings.all()


def tenant_provisioned(tenant_id):
    """Returns true if any networks or ports exist for a tenant."""
    session = db.get_reader_session()
    with session.begin():
        network_model = models_v2.Network
        port_model = models_v2.Port
        res = bool(
            session.query(network_model).filter_by(tenant_id=tenant_id).count()
            or session.query(port_model).filter_by(tenant_id=tenant_id).count()
        )
    return res


def instance_provisioned(device_id):
    """Returns true if any ports exist for an instance."""
    session = db.get_reader_session()
    with session.begin():
        port_model = models_v2.Port
        res = bool(session.query(port_model)
                   .filter(port_model.device_id == device_id).count())
    return res


def port_provisioned(port_id):
    """Returns true if port still exists."""
    session = db.get_reader_session()
    with session.begin():
        port_model = models_v2.Port
        res = bool(session.query(port_model)
                   .filter(port_model.id == port_id).count())
    return res


def get_port_binding_level(filters):
    """Returns entries from PortBindingLevel based on the specified filters."""
    session = db.get_reader_session()
    with session.begin():
        return (session.query(ml2_models.PortBindingLevel).
                filter_by(**filters).
                order_by(ml2_models.PortBindingLevel.level).
                all())


class NeutronNets(db_base_plugin_v2.NeutronDbPluginV2,
                  sec_db.SecurityGroupDbMixin):
    """Access to Neutron DB.

    Provides access to the Neutron Data bases for all provisioned
    networks as well ports. This data is used during the synchronization
    of DB between ML2 Mechanism Driver and Arista EOS
    Names of the networks and ports are not stroed in Arista repository
    They are pulled from Neutron DB.
    """

    def __init__(self):
        self.admin_ctx = nctx.get_admin_context()

    def get_all_networks_for_tenant(self, tenant_id):
        filters = {'tenant_id': [tenant_id]}
        return super(NeutronNets,
                     self).get_networks(self.admin_ctx, filters=filters) or []

    def get_all_networks(self):
        return super(NeutronNets, self).get_networks(self.admin_ctx) or []

    def get_all_ports_for_tenant(self, tenant_id):
        filters = {'tenant_id': [tenant_id]}
        return super(NeutronNets,
                     self).get_ports(self.admin_ctx, filters=filters) or []

    def get_shared_network_owner_id(self, network_id):
        filters = {'id': [network_id]}
        nets = self.get_networks(self.admin_ctx, filters=filters) or []
        segments = segments_db.get_network_segments(self.admin_ctx,
                                                    network_id)
        if not nets or not segments:
            return
        if (nets[0]['shared'] and
           segments[0][driver_api.NETWORK_TYPE] == n_const.TYPE_VLAN):
            return nets[0]['tenant_id']

    def get_network_segments(self, network_id, dynamic=False, context=None):
        context = context if context is not None else self.admin_ctx
        segments = segments_db.get_network_segments(context, network_id,
                                                    filter_dynamic=dynamic)
        if dynamic:
            for segment in segments:
                segment['is_dynamic'] = True
        return segments

    def get_all_network_segments(self, network_id, context=None):
        segments = self.get_network_segments(network_id, context=context)
        segments += self.get_network_segments(network_id, dynamic=True,
                                              context=context)
        return segments

    def get_segment_by_id(self, context, segment_id):
        return segments_db.get_segment_by_id(context,
                                             segment_id)

    def get_network_from_net_id(self, network_id, context=None):
        filters = {'id': [network_id]}
        ctxt = context if context else self.admin_ctx
        return super(NeutronNets,
                     self).get_networks(ctxt, filters=filters) or []

    def get_subnet_info(self, subnet_id):
        return self.get_subnet(subnet_id)

    def get_subnet_ip_version(self, subnet_id):
        subnet = self.get_subnet(subnet_id)
        return subnet['ip_version'] if 'ip_version' in subnet else None

    def get_subnet_gateway_ip(self, subnet_id):
        subnet = self.get_subnet(subnet_id)
        return subnet['gateway_ip'] if 'gateway_ip' in subnet else None

    def get_subnet_cidr(self, subnet_id):
        subnet = self.get_subnet(subnet_id)
        return subnet['cidr'] if 'cidr' in subnet else None

    def get_network_id(self, subnet_id):
        subnet = self.get_subnet(subnet_id)
        return subnet['network_id'] if 'network_id' in subnet else None

    def get_network_id_from_port_id(self, port_id):
        port = self.get_port(port_id)
        return port['network_id'] if 'network_id' in port else None

    def get_subnet(self, subnet_id):
        return super(NeutronNets,
                     self).get_subnet(self.admin_ctx, subnet_id) or {}

    def get_port(self, port_id):
        return super(NeutronNets,
                     self).get_port(self.admin_ctx, port_id) or {}

    def get_all_security_gp_to_port_bindings(self):
        return super(NeutronNets, self)._get_port_security_group_bindings(
            self.admin_ctx) or []

    def get_security_gp_to_port_bindings(self, sec_gp_id):
        filters = {'security_group_id': [sec_gp_id]}
        return super(NeutronNets, self)._get_port_security_group_bindings(
            self.admin_ctx, filters=filters) or []

    def get_security_group(self, sec_gp_id):
        return super(NeutronNets,
                     self).get_security_group(self.admin_ctx, sec_gp_id) or []

    def get_security_groups(self):
        sgs = super(NeutronNets,
                    self).get_security_groups(self.admin_ctx) or []
        sgs_all = {}
        if sgs:
            for s in sgs:
                sgs_all[s['id']] = s
        return sgs_all

    def get_security_group_rule(self, sec_gpr_id):
        return super(NeutronNets,
                     self).get_security_group_rule(self.admin_ctx,
                                                   sec_gpr_id) or []

    def validate_network_rbac_policy_change(self, resource, event, trigger,
                                            context, object_type, policy,
                                            **kwargs):
        return super(NeutronNets, self).validate_network_rbac_policy_change(
            resource, event, trigger, context, object_type, policy, kwargs)
