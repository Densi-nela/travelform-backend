"""Safe delivery diagnostics: never expose URLs, credentials or response bodies."""
import socket
import ssl
from urllib.error import HTTPError, URLError


def delivery_error_code(exc):
    if isinstance(exc, HTTPError):
        return f'HTTPError:{exc.code}'
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, socket.gaierror):
        return 'DNSResolutionError'
    if isinstance(reason, ssl.SSLError):
        return 'TLSConnectionError'
    if isinstance(reason, TimeoutError):
        return 'TimeoutError'
    if isinstance(reason, ConnectionRefusedError):
        return 'ConnectionRefusedError'
    return type(exc).__name__


def delivery_error_message(code):
    messages = {
        'DNSResolutionError': 'Cannot resolve the notification service hostname. Check the Django server company network/VPN connection and internal DNS.',
        'TLSConnectionError': 'Cannot establish a secure connection to the notification service. Check its certificate and server trust configuration.',
        'TimeoutError': 'The notification service timed out. Check connectivity and service availability before retrying.',
        'ConnectionRefusedError': 'The notification service refused the connection. Check that the service is running on the configured port.',
        'URLError': 'Cannot connect to the notification service. Check the service URL, network/VPN and firewall.',
        'NoCredentialsError': 'AWS credentials are unavailable. Configure credentials for SQS or enable API-only email delivery.',
    }
    if code.startswith('HTTPError:'):
        status = code.split(':', 1)[1]
        if status.isdigit():
            return f'The notification service returned HTTP {status}. Check service configuration and availability.'
    return messages.get(code, 'Email delivery failed. Check the notification service configuration before retrying.')
