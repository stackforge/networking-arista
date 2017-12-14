# Copyright (c) 2017 OpenStack Foundation
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

from neutron_lib.plugins import constants as plugin_constants
from neutron_lib.plugins import directory
from oslo_utils import importutils

from neutron.tests.unit import testlib_api

from networking_arista.common import db_lib
from networking_arista.tests.unit import utils


class DbLibTest(testlib_api.SqlTestCase):
    """Test cases for database helper functions."""

    def setUp(self):
        super(DbLibTest, self).setUp()
        plugin_klass = importutils.import_class(
            "neutron.db.db_base_plugin_v2.NeutronDbPluginV2")
        directory.add_plugin(plugin_constants.CORE, plugin_klass())

    def test_get_tenants_empty(self):
        tenants = db_lib.get_tenants.__func__()
        self.assertEqual(tenants, [])

    def test_get_tenants_from_networks(self):
        tenant_1_id = 't1'
        tenant_2_id = 't2'
        utils.create_networks([{'id': 'n1',
                                'project_id': tenant_1_id},
                               {'id': 'n2',
                                'project_id': tenant_2_id}])
        tenants = db_lib.get_tenants.__func__()
        expected_tenants = [{'project_id': tenant_1_id},
                            {'project_id': tenant_2_id}]
        self.assertItemsEqual(tenants, expected_tenants)

    def test_get_tenants_with_shared_network_ports(self):
        tenant_1_id = 't1'
        tenant_2_id = 't2'
        utils.create_networks([{'id': 'n1',
                                'project_id': tenant_1_id}])
        utils.create_ports([{'admin_state_up': True,
                             'status': 'ACTIVE',
                             'device_id': 'vm1',
                             'device_owner': 'compute:None',
                             'tenant_id': tenant_2_id,
                             'id': 'p1',
                             'network_id': 'n1',
                             'mac_address': '00:00:00:00:00:01'}])
        tenants = db_lib.get_tenants.__func__()
        expected_tenants = [{'project_id': tenant_1_id},
                            {'project_id': tenant_2_id}]
        self.assertItemsEqual(tenants, expected_tenants)

    def test_get_tenants_uniqueness(self):
        tenant_1_id = 't1'
        tenant_2_id = 't2'
        utils.create_networks([{'id': 'n1',
                                'project_id': tenant_1_id},
                               {'id': 'n2',
                                'project_id': tenant_2_id}])
        utils.create_ports([{'admin_state_up': True,
                             'status': 'ACTIVE',
                             'device_id': 'vm1',
                             'device_owner': 'compute:None',
                             'tenant_id': tenant_1_id,
                             'id': 'p1',
                             'network_id': 'n1',
                             'mac_address': '00:00:00:00:00:01'},
                            {'admin_state_up': True,
                             'status': 'ACTIVE',
                             'device_id': 'vm2',
                             'device_owner': 'compute:None',
                             'tenant_id': tenant_2_id,
                             'id': 'p2',
                             'network_id': 'n2',
                             'mac_address': '00:00:00:00:00:02'}])
        tenants = db_lib.get_tenants.__func__()
        expected_tenants = [{'project_id': tenant_1_id},
                            {'project_id': tenant_2_id}]
        self.assertItemsEqual(tenants, expected_tenants)

    def test_get_tenants_port_network_union(self):
        tenant_1_id = 't1'
        tenant_2_id = 't2'
        tenant_3_id = 't3'
        tenant_4_id = 't4'
        utils.create_networks([{'id': 'n1',
                                'project_id': tenant_1_id},
                               {'id': 'n2',
                                'project_id': tenant_2_id}])
        utils.create_ports([{'admin_state_up': True,
                             'status': 'ACTIVE',
                             'device_id': 'vm1',
                             'device_owner': 'compute:None',
                             'tenant_id': tenant_3_id,
                             'id': 'p1',
                             'network_id': 'n1',
                             'mac_address': '00:00:00:00:00:01'},
                            {'admin_state_up': True,
                             'status': 'ACTIVE',
                             'device_id': 'vm2',
                             'device_owner': 'compute:None',
                             'tenant_id': tenant_4_id,
                             'id': 'p2',
                             'network_id': 'n2',
                             'mac_address': '00:00:00:00:00:02'}])

        tenants = db_lib.get_tenants.__func__()
        expected_tenants = [{'project_id': tenant_1_id},
                            {'project_id': tenant_2_id},
                            {'project_id': tenant_3_id},
                            {'project_id': tenant_4_id}]
        self.assertItemsEqual(tenants, expected_tenants)

    def test_tenant_provisioned(self):
        tenant_1_id = 't1'
        port_1_id = 'p1'
        tenant_2_id = 't2'
        port_2_id = 'p2'
        network_id = 'network-id'
        self.assertFalse(db_lib.tenant_provisioned(tenant_1_id))
        self.assertFalse(db_lib.tenant_provisioned(tenant_2_id))
        n_ctx = utils.create_network(tenant_1_id, network_id, 11, shared=True)
        self.assertTrue(db_lib.tenant_provisioned(tenant_1_id))
        self.assertFalse(db_lib.tenant_provisioned(tenant_2_id))
        utils.create_port(tenant_1_id, network_id, 'vm1', port_1_id, n_ctx)
        self.assertTrue(db_lib.tenant_provisioned(tenant_1_id))
        self.assertFalse(db_lib.tenant_provisioned(tenant_2_id))
        utils.create_port(tenant_2_id, network_id, 'vm2', port_2_id, n_ctx)
        self.assertTrue(db_lib.tenant_provisioned(tenant_1_id))
        self.assertTrue(db_lib.tenant_provisioned(tenant_2_id))
        utils.delete_port(port_1_id)
        utils.delete_port(port_2_id)
        utils.delete_network(network_id)
        self.assertFalse(db_lib.tenant_provisioned(tenant_1_id))
        self.assertFalse(db_lib.tenant_provisioned(tenant_2_id))
