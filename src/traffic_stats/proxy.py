"""Discover local Docker proxy endpoints without reading container secrets."""
import errno
import ipaddress
import re

from .common import loads, run


DOCKER = ('/usr/bin/docker', '-H', 'unix:///var/run/docker.sock')
CONTAINER_FORMAT = ('{"name":{{json .Name}},"image":{{json .Config.Image}},'
                    '"state":{{json .State.Status}},"running":{{json .State.Running}},'
                    '"mode":{{json .HostConfig.NetworkMode}},'
                    '"networks":{{json .NetworkSettings.Networks}},'
                    '"ports":{{json .NetworkSettings.Ports}}}')


def names(values):
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ValueError('Proxy containers must be a list of container names')
    result = []
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}', value):
            raise ValueError('Invalid proxy container name')
        if value not in result:
            result.append(value)
    return result


def _container_ids(cancel):
    raw = run([*DOCKER, 'container', 'ls', '--all', '--quiet', '--no-trunc'], cancel=cancel)
    try:
        identities = raw.decode('ascii').split()
    except UnicodeError as error:
        raise ValueError('Invalid Docker container list') from error
    if any(not re.fullmatch(r'[0-9a-f]{64}', identity) for identity in identities):
        raise ValueError('Invalid Docker container ID')
    return list(dict.fromkeys(identities))


def _inspect(identities, cancel):
    records = []
    for offset in range(0, len(identities), 128):
        # Project only network and display fields; full inspect includes environment secrets.
        output = run([*DOCKER, 'container', 'inspect', '--format', CONTAINER_FORMAT,
                      *identities[offset:offset + 128]], cancel=cancel)
        rows = output.splitlines()
        if len(rows) != len(identities[offset:offset + 128]):
            raise ValueError('Incomplete Docker container inspection')
        records.extend(loads(row) for row in rows)
    return records


def _listeners(ports):
    if ports is None:
        return []
    if not isinstance(ports, dict):
        raise ValueError('Invalid Docker published ports')
    listeners = set()
    for key, bindings in ports.items():
        match = re.fullmatch(r'([0-9]+)/([a-z]+)', key)
        if not match or not 1 <= int(match[1]) <= 65535:
            raise ValueError('Invalid Docker container port')
        if match[2] not in ('tcp', 'udp') or bindings is None:
            continue
        if not isinstance(bindings, list):
            raise ValueError('Invalid Docker port bindings')
        for binding in bindings:
            if not isinstance(binding, dict):
                raise ValueError('Invalid Docker port binding')
            address, port = binding.get('HostIp', ''), binding.get('HostPort')
            if address == '':
                address = '0.0.0.0'
            if (not isinstance(address, str) or '%' in address or not isinstance(port, str) or
                    not re.fullmatch(r'[0-9]{1,5}', port) or not 1 <= int(port) <= 65535):
                raise ValueError('Invalid Docker published listener')
            listeners.add((str(ipaddress.ip_address(address)), match[2], int(port)))
    return [{'address': address, 'protocol': protocol, 'port': port}
            for address, protocol, port in sorted(listeners)]


def containers(cancel=None):
    identities = _container_ids(cancel)
    try:
        records = _inspect(identities, cancel)
    except OSError as error:
        if error.errno != errno.EIO:
            raise
        # A container may disappear between ls and inspect. Retry only a changed list.
        refreshed = _container_ids(cancel)
        if set(refreshed) == set(identities):
            raise
        records = _inspect(refreshed, cancel)
    result, excluded = [], []
    for record in records:
        if not isinstance(record, dict) or not all(isinstance(record.get(key), str)
                                                  for key in ('name', 'image', 'state', 'mode')):
            raise ValueError('Invalid Docker container metadata')
        name = names([record['name'].removeprefix('/')])[0]
        if record['mode'] in ('host', 'none') or record['mode'].startswith('container:'):
            excluded.append(name)
            continue
        if type(record.get('running')) is not bool:
            raise ValueError('Invalid Docker container state')
        endpoints, listeners = [], []
        if record['running']:
            networks = record.get('networks')
            if networks is None:
                networks = {}
            if not isinstance(networks, dict):
                raise ValueError('Invalid Docker container networks')
            for network in networks.values():
                if not isinstance(network, dict):
                    raise ValueError('Invalid Docker network endpoint')
                endpoints.extend(network[key] for key in ('IPAddress', 'GlobalIPv6Address') if network.get(key))
            endpoints = addresses(endpoints)
            listeners = _listeners(record.get('ports'))
        result.append({'name': name, 'image': record['image'], 'state': record['state'],
                       'addresses': endpoints, 'listeners': listeners})
    return {'containers': sorted(result, key=lambda item: item['name']), 'excluded_names': sorted(set(excluded))}


def addresses(values):
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ValueError("Proxy container IPs must be a list of host addresses")
    result = []
    for value in values:
        if not isinstance(value, str) or '%' in value:
            raise ValueError("Invalid proxy container IP")
        address = ipaddress.ip_address(value)
        if address.is_unspecified or address.is_multicast or address.is_loopback or address.is_link_local:
            raise ValueError("Proxy container IP must be a unicast host address")
        if str(address) not in result:
            result.append(str(address))
    return result
