#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# engine_page.py - Copyright (C) 2012 Red Hat, Inc.
# Written by Fabian Deutsch <fabiand@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301, USA.  A copy of the GNU General Public License is
# also available at http://www.gnu.org/copyleft/gpl.html.
import augeas
import ConfigParser
import errno
import httplib
import os
import sys
import traceback

from . import config
from socket import error as socket_error

from ovirt.node import plugins, valid, ui, utils, log
from ovirt.node.config.defaults import NodeConfigFileSection, SSH, Management
from ovirt.node.plugins import Changeset
from ovirt.node.utils.fs import Config

from vdsm import vdscli

"""
Configure Engine
"""


LOGGER = log.getLogger(__name__)


def update_conf(conf_file, session, option, value):
    """
    Update configuration file

    Args:
    conf_file -- Configuration file
    session -- Session in the configuration file
    option -- option to be updated
    value -- value to be updated in the option value

    Returns: True or False
    """

    if not os.path.exists(conf_file):
        raise IOError('Cannot find file to be updated.')

    try:
        cfg = ConfigParser.RawConfigParser()
        cfg.read(conf_file)
        cfg.set(session, option, value)

        with open(conf_file, 'w+') as configfile:
            cfg.write(configfile)
    except ConfigParser.Error:
        LOGGER.debug("Cannot write into file!", exc_info=True)
        raise

    return True


def sync_mgmt():
    """Guess mgmt interface and update TUI config
       FIXME: Autoinstall should write MANAGED_BY and MANAGED_IFNAMES
       into /etc/defaults/ovirt
    """
    engine_data = None
    cfg = VDSM().retrieve()

    mgmtIface = []

    try:
        cli = vdscli.connect()
        networks = cli.getVdsCapabilities()['info']['networks']

        for net in networks:
            if net in ('ovirtmgmt', 'rhevm'):
                if 'bridge' in networks[net]:
                    mgmtIface = [networks[net]['bridge']]
                else:
                    mgmtIface = [networks[net]['iface']]
    except socket_error as err:
        if err.errno == errno.ECONNREFUSED:
            LOGGER.debug("Connection refused with VDSM", exc_info=True)
        elif err.errno == errno.ENETUNREACH:
            LOGGER.debug("Network is unreachable to reach VDSM", exc_info=True)
        else:
            raise
    except Exception as err:
        if 'No permission to read file:' in str(err):
            LOGGER.debug("pem files not available yet!", exc_info=True)
        else:
            raise

    if cfg["server"] is not None:
        server_url = [unicode(info) for info in [cfg["server"], cfg["port"]] if info]
        port, sslPort = compatiblePort(cfg["port"])
        if sslPort:
            proto = "https"
        else:
            proto = "http"
        engine_data = '"%s %s://%s"' % (
            config.engine_name,
            proto,
            ":".join(server_url)
        )

    ag = augeas.Augeas()
    ag.set("/augeas/save/copy_if_rename_fails", "")
    ag.set("/files/etc/default/ovirt/MANAGED_IFNAMES", "\"%s\"" %
           ''.join(mgmtIface).encode('utf-8'))
    ag.set("/files/etc/default/ovirt/OVIRT_MANAGEMENT_SERVER", "\"%s\"" %
           cfg["server"])
    ag.set("/files/etc/default/ovirt/OVIRT_MANAGEMENT_PORT", "\"%s\"" %
           cfg["port"])
    if engine_data is not None:
        ag.set("/files/etc/default/ovirt/MANAGED_BY",
               engine_data.encode('utf-8'))
    ag.save()


class Plugin(plugins.NodePlugin):
    _cert_path = None
    _server = None
    _port = None
    _model = None

    def __init__(self, app):
        super(Plugin, self).__init__(app)
        self._model = {}
        sync_mgmt()

    def name(self):
        return config.engine_name

    def rank(self):
        return 100

    def model(self):
        cfg = VDSM().retrieve()
        model = {
            "vdsm_cfg.address": cfg["server"] or "",
            "vdsm_cfg.port": cfg["port"] or "443",
            "vdsm_cfg.cert": "Verified"
            if utils.fs.Config().exists(cfg["cert_path"]) else "N/A",
            "vdsm_cfg.password": "",
        }
        return model

    def validators(self):

        return {"vdsm_cfg.address": valid.FQDNOrIPAddress() | valid.Empty(),
                "vdsm_cfg.port": valid.Port(),
                "vdsm_cfg.password": valid.Text(),
                }

    def ui_content(self):
        sync_mgmt()

        buttons = []
        net_is_configured = utils.network.NodeNetwork().is_configured()
        header_menu = "{engine_name} Configuration".format(
            engine_name=config.engine_name
        )

        if not net_is_configured:
            ws = [
                ui.Header(
                    "header[0]",
                    header_menu
                ),
                ui.Notice("network.notice",
                          "Networking is not configured, " +
                          "please configure it before " +
                          "registering"),
                ui.Divider("divider[0]")
            ]
        else:
            ws = [
                ui.Header(
                    "header[0]",
                    header_menu
                ),
                ui.Entry("vdsm_cfg.address", "Management Server:"),
                ui.Entry("vdsm_cfg.port", "Management Server Port:"),
                ui.Divider("divider[0]"),
                ui.KeywordLabel("vdsm_cfg.cert", "Certificate Status: "),
                ui.Divider("divider[1]"),
                ui.Label("vdsm_cfg.password._label",
                         "Optional password for adding Node through " +
                         str(config.engine_name)),
                ui.Label("vdsm_cfg.password._label2",
                         "Note: Setting password will enable SSH daemon"),
                ui.ConfirmedEntry("vdsm_cfg.password", "Password:", True),
            ]
            buttons = [
                ui.SaveButton(
                    "action.register",
                    "Save & Register"
                )
            ]

        page = ui.Page("page", ws)
        page.buttons = buttons

        self.widgets.add(page)
        return page

    def on_change(self, changes):
        if changes.contains_any(["vdsm_cfg.password"]):
            self._model.update(changes)

    def on_merge(self, effective_changes):
        model = VDSM()
        self.logger.info("Saving engine stuff")
        changes = Changeset(self.pending_changes(False))
        effective_model = Changeset(self.model())
        effective_model.update(effective_changes)

        self.logger.debug("Changes: %s" % changes)
        self.logger.debug("Effective Model: %s" % effective_model)

        cfg_port = effective_model["vdsm_cfg.port"]
        cfg_server = effective_model["vdsm_cfg.address"]
        compat, ssl_port = compatiblePort(cfg_port)
        buttons = [ui.Button("action.cert.accept", "Accept"),
                   ui.Button("action.cert.reject", "Reject & Remove")]

        txs = utils.Transaction(
            "Configuring {engine_name}".format(
            engine_name=config.engine_name
            )
        )

        if cfg_server and ssl_port and \
                effective_changes.contains_any(["action.register"]):
            title_msg = "\n"
            try:
                port = findPort(cfg_server, cfg_port)
                self._server, self._port = cfg_server, port
                self._cert_path, fingerprint = retrieveCetrificate(
                    cfg_server,
                    port
                )

                if changes.contains_any(["vdsm_cfg.password"]):
                    title_msg = "\nSetting root password and starting sshd\n\n"
                    SetRootPassword(
                        password=effective_model["vdsm_cfg.password"]
                    ).commit()

                title_msg += "Please review the following SSL fingerprint" \
                             " from Engine:\n"
            except Exception as e:
                fingerprint = str(e)
                title_msg = "\n"
                buttons = [ui.Button("action.cert.reject", "Close")]

            self._fp_dialog = ui.Dialog("dialog.engine.fp", "{engine_name} "
                                        "Fingerprint".format(
                                            engine_name=config.engine_name
                                        ),
                                        [ui.Label(
                                            "dialog.label[0]",
                                            title_msg),
                                         ui.Label("dialog.fp", fingerprint)])

            self._fp_dialog.buttons = buttons
            return self._fp_dialog

        elif effective_model.contains_any(["action.cert.accept"]):
            self._fp_dialog.close()
            model.update(self._server, self._port, self._cert_path)
            utils.fs.Config().persist(self._cert_path)
            txs += [ActivateVDSM(effective_model["vdsm_cfg.address"],
                                 effective_model["vdsm_cfg.port"])]
            self._server, self._port, self._cert_path = None, None, None

        elif changes.contains_any(["action.cert.reject"]):
            model.update(cert_path=None)
            utils.fs.Config().unpersist(self._cert_path)
            if self._cert_path is not None:
                os.unlink(self._cert_path)
            self._fp_dialog.close()
            self._server, self._port, self._cert_path = None, None, None

        if changes.contains_any(["vdsm_cfg.password"]):
            txs += [SetRootPassword(
                password=effective_model["vdsm_cfg.password"]
            )]

        if effective_changes.contains_any(["action.register"]) and \
                effective_model["vdsm_cfg.address"] != "":
            self.logger.debug("Connecting to engine")

            try:
                effective_model["vdsm_cfg.port"] = findPort(cfg_server,
                                                            cfg_port)
                txs += [
                    ActivateVDSM(
                        effective_model["vdsm_cfg.address"],
                        effective_model["vdsm_cfg.port"]
                    )
                ]
            except Exception as e:
                txs += [CannotFindEngine()]

            model.update(
                server=effective_model["vdsm_cfg.address"],
                port=effective_model["vdsm_cfg.port"]
            )
            self._server = effective_model["vdsm_cfg.address"]
            self._port = effective_model["vdsm_cfg.port"]

        if len(txs) > 0:
            progress_dialog = ui.TransactionProgressDialog("dialog.txs", txs,
                                                           self)
            progress_dialog.run()

            # VDSM messes with logging, and we just reset it
            log.configure_logging()

        # Acts like a page reload
        return self.ui_content()


def findPort(engineServer, enginePort):
    """Function to find the correct port for a given server
    """
    _TIMEOUT_FIND_HOST_SEC = 5

    # pylint: disable-msg=E0611,F0401
    sys.path.append('/usr/share/vdsm-reg')
    import deployUtil  # @UnresolvedImport

    compatPort, sslPort = compatiblePort(enginePort)

    LOGGER.debug("Finding port %s:%s with compat %s ssl %s" %
                 (engineServer, enginePort, compatPort, sslPort))

    deployUtil.nodeCleanup()

    # Build port list to try
    port_cfgs = [(enginePort, sslPort)]
    if compatPort:
        port_cfgs += [(compatPort, sslPort)]
    else:
        port_cfgs += [(enginePort, False)]

    LOGGER.debug("Port configuratoins for engine: %s" % port_cfgs)

    for try_port, use_ssl in port_cfgs:
        LOGGER.debug("Trying to reach engine %s via %s %s" %
                     (engineServer, try_port, "SSL" if use_ssl else ""))

        is_reachable = False

        try:
            is_reachable = isHostReachable(host=engineServer,
                                           port=try_port, ssl=use_ssl,
                                           timeout=_TIMEOUT_FIND_HOST_SEC)
        except Exception:
            LOGGER.debug("Failed to reach engine: %s" % traceback.format_exc())

        if is_reachable:
            LOGGER.debug("Reached engine")
            enginePort = try_port
            break

    if not is_reachable:
        raise RuntimeError("Can't connect to {engine_name}".format(
            engine_name=config.engine_name))

    return enginePort


def compatiblePort(portNumber):
    """
    Until the version 3.0, oVirt Engine provided port 8443/8080 to oVirt Node
    download cert and others files. Since 3.1 the default port changed to
    443/80. This function, will return the compatible port in case the VDSM
    cannot communicate with oVirt Engine.

    :param portNumber: port which doesn't communicate with oVirt Engine
    :returns: compatible port number (or None if there is no compatible port)
              and if it's SSL port or not (bool)
    """

    compatPort = {
        '443': ('8443', True),
        '8443': ('443', True),
        '80': ('8080', False),
        '8080': ('80', False)
    }

    return compatPort.get(portNumber, (None, False))


def isHostReachable(host, port, ssl, timeout):
    """Check if a host is reachable on a given port via HTTP/HTTPS
    """
    if ssl:
        Connection = httplib.HTTPSConnection
    else:
        Connection = httplib.HTTPConnection
    Connection(str(host), port=int(port), timeout=timeout).request("HEAD", "/")
    return True


def retrieveCetrificate(engineServer, enginePort):
    """Function to retrieve and store the certificate from an Engine
    """
    fingerprint = None

    # pylint: disable-msg=E0611,F0401
    sys.path.append('/usr/share/vdsm-reg')
    import deployUtil  # @UnresolvedImport
    # pylint: enable-msg=E0611,F0401

    if deployUtil.getRhevmCert(engineServer, enginePort):
        _, _, path = deployUtil.certPaths('')
        fingerprint = deployUtil.generateFingerPrint(path)
    else:
        msgCert = "Failed downloading " \
            "{engine_name} certificate".format(
                engine_name=config.engine_name)
        raise RuntimeError(msgCert)

    return path, fingerprint


#
#
# Functions and classes to support the UI
#
#
class VDSM(NodeConfigFileSection):
    """Class to handle VDSM configuration in /etc/default/ovirt file

    >>> from ovirt.node.config.defaults import ConfigFile, SimpleProvider
    >>> fn = "/tmp/cfg_dummy"
    >>> cfgfile = ConfigFile(fn, SimpleProvider)
    >>> n = VDSM(cfgfile)
    >>> n.update("engine.example.com", "1234", "p")
    >>> sorted(n.retrieve().items())
    [('cert_path', 'p'), ('port', '1234'), ('server', 'engine.example.com')]
    """
    keys = ("OVIRT_MANAGEMENT_SERVER",
            "OVIRT_MANAGEMENT_PORT",
            "OVIRT_MANAGEMENT_CERTIFICATE")

    @NodeConfigFileSection.map_and_update_defaults_decorator
    def update(self, server, port, cert_path):
        (valid.Empty(or_none=True) | valid.FQDNOrIPAddress())(server)
        (valid.Empty(or_none=True) | valid.Port())(port)


class CannotFindEngine(utils.Transaction.Element):
    title = "Trying to connect with {engine_name}..".format(
        engine_name=config.engine_name
    )

    def commit(self):
        raise RuntimeError(
            "Cannot connect with management system!"
        )


class SetRootPassword(utils.Transaction.Element):
    title = "Setting root password and starting sshd"

    def __init__(self, password):
        super(SetRootPassword, self).__init__()
        self.password = password

    def commit(self):
        passwd = utils.security.Passwd()
        passwd.set_password("root", self.password)

        sshcfg = SSH()
        sshcfg.update(pwauth=True)
        sshcfg.commit()


class ActivateVDSM(utils.Transaction.Element):
    title = "Activating VDSM"

    def __init__(self, server, port):
        super(ActivateVDSM, self).__init__()

        if not os.path.exists(config.VDSM_CONFIG):
            self.create_vdsm_conf()
            self.logger.info("Agent configuration files created.")

        self.server = server
        self.port = port

    def cert_validator(self):
        cert_path = VDSM().retrieve()["cert_path"]
        cert_exists = cert_path and os.path.exists(cert_path)

        return cert_exists

    def create_vdsm_conf(self):
        cfg = ConfigParser.RawConfigParser()

        cfg.add_section('addresses')
        cfg.set('addresses', 'management_port', '54321')

        cfg.add_section('vars')
        cfg.set('vars', 'trust_store_path', '/etc/pki/vdsm/')
        cfg.set('vars', 'ssl', 'true')

        with open(config.VDSM_CONFIG, 'w') as configfile:
            cfg.write(configfile)

        Config().persist(config.VDSM_CONFIG)

    def commit(self):
        self.logger.info("Connecting to VDSM server")

        if not self.cert_validator():
            self.logger.info("Trying register without validating cert..")

        # pylint: disable-msg=E0611,F0401
        sys.path.append('/usr/share/vdsm-reg')
        import deployUtil  # @UnresolvedImport

        sys.path.append('/usr/share/vdsm')
        from vdsm import constants

        cfg = VDSM().retrieve()

        # Stopping vdsm-reg may fail but its ok - its in the case when the
        # menus are run after installation
        self.logger.info("Stopping vdsm-reg service")
        deployUtil._logExec([constants.EXT_SERVICE, 'vdsm-reg', 'stop'])

        try:
            update_conf(
                config.VDSM_REG_CONFIG,
                "vars",
                "vdc_host_name",
                cfg["server"]
            )

            update_conf(
                config.VDSM_REG_CONFIG,
                "vars",
                "vdc_host_port",
                cfg["port"]
            )
        except (ConfigParser.Error, IOError):
            mgmt = Management()
            mgmt.clear()

            msgConf = "{engine_name} Configuration Failed".format(
                engine_name=config.engine_name
            )
            self.logger.info(msgConf, exc_info=True)
            raise RuntimeError(msgConf)

        self.logger.info("Starting vdsm-reg service")
        deployUtil._logExec([constants.EXT_SERVICE, 'vdsm-reg', 'start'])
        sync_mgmt()

        msgConf = "{engine_name} Configuration Successfully Updated".format(
            engine_name=config.engine_name
        )
        self.logger.debug(msgConf)
