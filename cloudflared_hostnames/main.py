import argparse
from collections import OrderedDict
import logging
import os
from queue import SimpleQueue
from threading import Thread
from typing import List, Dict, Optional
from urllib.parse import urlparse

import docker
from docker.types.daemon import CancellableStream

from .cloudflare_api import CloudflareApi, DnsRecordType
from .api import Api, CachedApi

LOGGER = logging.getLogger('cfd-hostnames')


class Params:
    def __init__(self, hostname: str, service: str, zone_name: str, zone_id: str, tunnel_id: str,
                 notlsverify: Optional[bool] = None):
        self.hostname = hostname
        self.service = service
        self.zone_name = zone_name
        self.zone_id = zone_id
        self.tunnel_id = tunnel_id
        self.notlsverify = notlsverify
        self.dns_type = DnsRecordType.CNAME


def docker_events_thread(events: CancellableStream, queue: SimpleQueue):
    for event in events:
        status = event.get('status')
        if event.get('Type') == 'container' and status in ('start', 'die'):
            queue.put(event)


def get_env_vars() -> List[str]:
    vals = []
    not_found = []
    os.environ['CLOUDFLARE_API_CERTKEY'] = ''
    for ev in ('CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_API_KEY', 'CLOUDFLARE_TUNNEL_ID'):
        val = os.environ.get(ev)
        if not val:
            not_found.append(ev)
        else:
            vals.append(val)
    if not_found:
        LOGGER.critical('%s environment variables are not set', ', '.join(not_found))
        raise SystemExit(1)
    return vals


def get_labels(labels: Dict[str, str]):
    return {key: val for key, val in labels.items() if key.startswith('cloudflare.')}


def validate_hostname(hostname: str):
    if hostname is not None:
        hostname = hostname.strip()
    if not hostname:
        raise Exception('hostname not specified')
    # TODO: use domain regex
    hostname_split = hostname.split('.')
    if len(hostname_split) < 2:
        raise Exception('hostname must be like "domain.com" or "subdomain.domain.com"')
    return hostname


def validate_service(service: str):
    if service is not None:
        service = service.strip()
    if not service:
        raise Exception('service not specified')
    parsed = urlparse(service)
    if not parsed.scheme or not parsed.netloc:
        raise Exception('service invalid')
    if parsed.scheme.lower() not in ('http', 'https'):
        raise Exception('service invalid scheme')
    return service





_trues = {'true', 'True', 'TRUE', 't', 'T', '1'}
_falses = {'false', 'False', 'FALSE', 'f', 'F', '0'}


def validate_notlsverify(val: str) -> Optional[bool]:
    if val is None:
        return None
    if val in _trues:
        return True
    if val in _falses:
        return False
    raise Exception(f'invalid notlsverify value: "{val}"')


def get_zone_name(hostname: str) -> str:
    return '.'.join(hostname.split('.')[-2:])


def get_params_from_labels(api: Api, default_tunnel_id: str, labels: Dict[str, str]) -> List[Params]:
    hostname = labels.get('cloudflare.zero_trust.access.tunnel.public_hostname')
    hostnames = OrderedDict.fromkeys(hostname.strip().split(','))
    hostnames = [validate_hostname(hn) for hn in hostnames.keys()]
    if not hostnames:
        raise Exception('hostname not specified')

    service = labels.get('cloudflare.zero_trust.access.tunnel.service')
    service = validate_service(service)

    zone_names = [get_zone_name(hn) for hn in hostnames]

    tunnel_id = labels.get('cloudflare.zero_trust.access.tunnel.id', default_tunnel_id)

    notlsverify = labels.get('cloudflare.zero_trust.access.tunnel.tls.notlsverify')
    notlsverify = validate_notlsverify(notlsverify)

    zone_ids = [api.get_zone_id(zone_name) for zone_name in zone_names]

    return [Params(hn, service, zone_names[ii], zone_ids[ii], tunnel_id, notlsverify) for ii, hn in
            enumerate(hostnames)]


def dns_record_value(tunnel_id: str) -> str:
    return f'{tunnel_id}.cfargotunnel.com'


def ingress_exists(params: Params, tunnel_ingress: List) -> bool:
    for item in tunnel_ingress:
        if item.get('hostname') == params.hostname:
            return True
    return False


def params_to_tunnel_ingress_entry(params):
    origin_request = {}
    if params.notlsverify is not None:
        origin_request['noTLSVerify'] = params.notlsverify
    return {
        'service': params.service,
        'hostname': params.hostname,
        'originRequest': origin_request,
    }


def add_tunnel_ingress(params: Params, tunnel_ingress: List):
    tunnel_ingress.insert(-1, params_to_tunnel_ingress_entry(params))
    LOGGER.info(f'Adding public hostname "%s" -> "%s" for tunnel "%s"', params.hostname, params.service,
                params.tunnel_id)


def container_add(api: Api, account_id: str, params: Params, new_dns_records: Dict[str, Params],
                  ingress_adds: Dict):
    records = api.get_dns_records(params.zone_id)
    if params.hostname in records:
        LOGGER.info('DNS record for "%s" already exists', params.hostname)
    elif params.hostname in new_dns_records:
        LOGGER.error('duplicate DNS record for "%s"', params.hostname)
    else:
        new_dns_records[params.hostname] = params

    tunnel_ingress = api.get_tunnel_ingress(account_id, params.tunnel_id)
    if ingress_exists(params, tunnel_ingress):
        LOGGER.info('Public hostname "%s" for tunnel "%s" already exists', params.hostname, params.tunnel_id)
    else:
        add_tunnel_ingress(params, tunnel_ingress)
        ingress_adds[(account_id, params.tunnel_id)] = tunnel_ingress

    return new_dns_records, ingress_adds


def containers_update_cf(args: argparse.Namespace, api: Api, new_dns_records: Dict[str, Params],
                         ingress_adds: Dict):
    for record_name, params in new_dns_records.items():
        val = dns_record_value(params.tunnel_id)
        LOGGER.info(f'Adding DNS record "%s" -> "%s"', record_name, val)
        if not args.dry_run:
            if not api.cf.create_dns_record(params.dns_type, params.zone_id, record_name, val):
                LOGGER.error('Failed to add DNS record "%s"', record_name)

    for keys, tunnel_ingress in ingress_adds.items():
        if not args.dry_run:
            if not api.cf.update_tunnel_configs(keys[0], keys[1],
                                                {'config': {'ingress': tunnel_ingress}}):
                LOGGER.error(f'Failed to update tunnel ingress for tunnel "{keys[1]}" failed')


def load_containers(args: argparse.Namespace, containers: List, api: Api, cf_account_id: str,
                    cf_tunnel_id: str):
    new_dns_records = {}
    ingress_adds = {}
    for container in containers:
        LOGGER.debug('inspecting container "%s"', container.name)
        if container.status != 'running':
            continue
        labels = get_labels(container.labels)
        if not labels:
            continue
        try:
            params = get_params_from_labels(api, cf_tunnel_id, labels)
            for pp in params:
                container_add(api, cf_account_id, pp, new_dns_records, ingress_adds)
        except Exception as exc:
            LOGGER.error('%s: %s', container.name, exc)

    containers_update_cf(args, api, new_dns_records, ingress_adds)


def handle_start_event(args: argparse.Namespace, api: Api, cf_account_id: str, params: Params):
    new_dns_records, ingress_adds = container_add(api, cf_account_id, params, {}, {})
    containers_update_cf(args, api, new_dns_records, ingress_adds)


def handle_die_event(args: argparse.Namespace, api: Api, account_id: str, params: Params):
    LOGGER.info(f'Removing DNS record "%s" for tunnel "%s"', params.hostname, params.tunnel_id)
    record_id = api.get_dns_record_id(params.zone_id, params.hostname)
    if record_id:
        if not args.dry_run:
            if not api.cf.delete_dns_record(params.zone_id, record_id):
                LOGGER.error('Failed to remove DNS record "%s" for tunnel "%s"', params.hostname, params.tunnel_id)
    else:
        LOGGER.warning('No DNS record "%s" for tunnel "%s"', params.hostname, params.tunnel_id)

    LOGGER.info(f'Removing public hostname "%s" for tunnel "%s"', params.hostname, params.tunnel_id)
    tunnel_ingress = api.get_tunnel_ingress(account_id, params.tunnel_id)
    before_len = len(tunnel_ingress)
    tunnel_ingress = [ii for ii in tunnel_ingress if ii.get('hostname') != params.hostname]
    if before_len > len(tunnel_ingress) and not args.dry_run:
        if not api.cf.update_tunnel_configs(account_id, params.tunnel_id,
                                            {'config': {'ingress': tunnel_ingress}}):
            LOGGER.error('Failed to remove public hostname "%s" for tunnel "%s"', params.hostname, params.tunnel_id)
    else:
        LOGGER.warning('No public hostname "%s" for tunnel "%s"', params.hostname, params.tunnel_id)


def main(args: argparse.Namespace):
    docker_events = None

    try:
        cf_account_id, cf_token, cf_tunnel_id = get_env_vars()

        cf = CloudflareApi(cf_token, debug=pargs.debug)
        docker_client = docker.from_env()

        queue = SimpleQueue()
        docker_events = docker_client.events(decode=True)
        thread = Thread(target=docker_events_thread, args=(docker_events, queue), name='docker_events')
        thread.start()

        LOGGER.info('Using tunnel ID "%s" as default tunnel', cf_tunnel_id)

        try:
            api = CachedApi(cf)
            load_containers(args, docker_client.containers.list(all=True), api, cf_account_id, cf_tunnel_id)
        except Exception as exc:
            LOGGER.critical('%s', exc)
            raise SystemExit(1)

        while True:
            event = queue.get()
            api = CachedApi(cf)

            try:
                status = event['status']
                attributes = event['Actor']['Attributes']
                labels = get_labels(attributes)
                if not labels:
                    continue

                container_name = attributes["name"]
                LOGGER.info('docker event "%s" for container "%s"', status, container_name)

                try:
                    params = get_params_from_labels(api, cf_tunnel_id, labels)
                except Exception as exc:
                    LOGGER.exception('%s: %s', container_name, exc)
                    continue

                if status == 'start':
                    for pp in params:
                        try:
                            handle_start_event(args, api, cf_account_id, pp)
                        except Exception as exc:
                            LOGGER.exception('%s: %s', container_name, exc)
                elif status == 'die':
                    for pp in params:
                        try:
                            handle_die_event(args, api, cf_account_id, pp)
                        except Exception as exc:
                            LOGGER.exception('%s: %s', container_name, exc)

            except Exception as exc:
                LOGGER.exception('invalid event: %s', exc)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        LOGGER.critical('failed: %s', exc)
    finally:
        if docker_events:
            docker_events.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Read docker container labels and automatically add hostnames '
                                                 'and DNS records to Cloudflare Zero Trust')
    parser.add_argument('-l', '--log-level', choices=('notset', 'debug', 'info', 'warning', 'error', 'critical'),
                        default='info', help='set the log level [info]')
    parser.add_argument('-d', '--dry-run', action='store_true',
                        help="don't run Cloudflare APIs that modify")
    parser.add_argument('--debug', action='store_true',
                        help='turn on Cloudflare API debug')
    pargs = parser.parse_args()

    pargs.log_level = getattr(logging, pargs.log_level.upper())

    logging.basicConfig(level=logging.DEBUG if pargs.debug else logging.INFO)
    LOGGER.setLevel(pargs.log_level)
    main(pargs)
