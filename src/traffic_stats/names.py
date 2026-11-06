"""Display-only UCI naming rules; device identity and accounting stay unchanged."""
import ipaddress
import re


def address(value):
    if not isinstance(value, str) or '%' in value:
        raise ValueError('Expected an IPv4 or IPv6 host address')
    return str(ipaddress.ip_address(value))


def rules(sections):
    result, invalid = [], []
    ordered = sorted(sections.items(), key=lambda pair: pair[1].get('.index', 0)
                     if isinstance(pair[1], dict) and type(pair[1].get('.index', 0)) is int else 0)
    for section_id, section in ordered:
        if not isinstance(section, dict) or section.get('.type') != 'device':
            continue
        try:
            name, mac, ip = (section.get(key, '') for key in ('name', 'mac', 'ip'))
            if any(not isinstance(value, str) for value in (name, mac, ip)):
                raise ValueError('Expected string options')
            name, mac, ip = name.strip(), mac.strip().lower(), ip.strip()
            if not name or len(name) > 128 or re.search(r'[\x00-\x1f\x7f]', name):
                raise ValueError('Invalid device name')
            if mac and not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
                raise ValueError('Invalid MAC address')
            if ip:
                ip = address(ip)
            if not mac and not ip:
                raise ValueError('A naming rule requires MAC or IP')
            result.append({'section': section_id, 'name': name, 'mac': mac, 'ip': ip})
        except ValueError:
            invalid.append(section_id)
    return result, invalid


def apply(devices, naming_rules):
    # Stable sorting makes the first configured rule win at equal specificity.
    ordered = sorted(naming_rules, key=lambda rule: (bool(rule['mac']) * 2 + bool(rule['ip'])), reverse=True)
    for device in devices:
        mac = (device.get('mac') or '').lower()
        addresses = set()
        for ip in device.get('addresses', []):
            try:
                addresses.add(address(ip))
            except ValueError:
                pass
        device['custom_name'] = ''
        device['name_rule'] = ''
        for rule in ordered:
            if (not rule['mac'] or rule['mac'] == mac) and (not rule['ip'] or rule['ip'] in addresses):
                device['custom_name'] = rule['name']
                device['name_rule'] = rule['section']
                break
