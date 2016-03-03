# Copyright (c) 2014 Thales Services SAS
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

from neutron.plugins.ml2 import config
from neutron.tests.unit import testlib_api

from opflexagent import config as ofconf  # noqa
from opflexagent import type_opflex


OPFLEX_NETWORKS = ['opflex_net1', 'opflex_net2']


class FlatTypeTest(testlib_api.SqlTestCase):

    def setUp(self):
        super(FlatTypeTest, self).setUp()
        config.cfg.CONF.set_override('opflex_networks', OPFLEX_NETWORKS,
                                     group='OPFLEX')
        self.driver = type_opflex.OpflexTypeDriver()
        self.driver.physnet_mtus = []

    def test_get_mtu(self):
        config.cfg.CONF.set_override('segment_mtu', 1475, group='ml2')
        config.cfg.CONF.set_override('path_mtu', 1400, group='ml2')
        self.driver.physnet_mtus = {'physnet1': 1450, 'physnet2': 1400}
        self.assertEqual(1450, self.driver.get_mtu('physnet1'))

        config.cfg.CONF.set_override('segment_mtu', 1375, group='ml2')
        config.cfg.CONF.set_override('path_mtu', 1400, group='ml2')
        self.driver.physnet_mtus = {'physnet1': 1450, 'physnet2': 1400}
        self.assertEqual(1375, self.driver.get_mtu('physnet1'))

        config.cfg.CONF.set_override('segment_mtu', 0, group='ml2')
        config.cfg.CONF.set_override('path_mtu', 1425, group='ml2')
        self.driver.physnet_mtus = {'physnet1': 1450, 'physnet2': 1400}
        self.assertEqual(1400, self.driver.get_mtu('physnet2'))

        config.cfg.CONF.set_override('segment_mtu', 0, group='ml2')
        config.cfg.CONF.set_override('path_mtu', 0, group='ml2')
        self.driver.physnet_mtus = {}
        self.assertEqual(0, self.driver.get_mtu('physnet1'))