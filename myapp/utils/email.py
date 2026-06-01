"""
Email notification utilities for Arcology.

Uses Python's stdlib smtplib so no extra dependency is required.
Configure via Flask app config (MAIL_* keys — compatible with Flask-Mail).
"""

import logging
import smtplib
import ssl
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


def _send_in_thread(subject, body_text, to_addr, app):
    """Send a plain-text email in a background thread."""
    cfg = app.config
    server  = cfg.get('MAIL_SERVER', 'localhost')
    port    = int(cfg.get('MAIL_PORT', 25))
    use_tls = cfg.get('MAIL_USE_TLS', False)
    use_ssl = cfg.get('MAIL_USE_SSL', False)
    username = cfg.get('MAIL_USERNAME') or None
    password = cfg.get('MAIL_PASSWORD') or None
    from_addr = cfg.get('MAIL_DEFAULT_SENDER', 'arcology@localhost')

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = from_addr
    msg['To']      = to_addr
    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))

    def _send():
        try:
            ctx = ssl.create_default_context() if (use_tls or use_ssl) else None
            if use_ssl:
                conn = smtplib.SMTP_SSL(server, port, context=ctx)
            else:
                conn = smtplib.SMTP(server, port)
                if use_tls:
                    conn.starttls(context=ctx)
            with conn:
                if username and password:
                    conn.login(username, password)
                conn.sendmail(from_addr, [to_addr], msg.as_string())
            log.info('Notification sent to %s: %s', to_addr, subject)
        except Exception as exc:
            log.warning('Failed to send notification to %s: %s', to_addr, exc)

    threading.Thread(target=_send, daemon=True).start()


def notify_analysis_complete(app, artefact, completed: int, failed: int):
    """Send an analysis-completion notification to the artefact owner.

    Silently does nothing if:
    - MAIL_SERVER is not configured
    - The owner has no email address
    - The owner has not enabled email_notifications in their preferences
    """
    if not app.config.get('MAIL_SERVER'):
        return

    owner = artefact.owner
    if not owner or not owner.email:
        return
    if not owner.get_preference('email_notifications'):
        return

    item_name = artefact.item.name if artefact.item else 'unknown item'
    status_line = f'{completed} completed, {failed} failed' if failed else f'{completed} completed'

    subject = f'[Arcology] Analysis complete: {artefact.label}'
    body = (
        f'Analysis has finished for artefact "{artefact.label}" '
        f'(item: {item_name}).\n\n'
        f'Result: {status_line}\n\n'
        f'You are receiving this because you enabled analysis notifications '
        f'in your Arcology profile.\n'
        f'To disable: visit your profile and uncheck "Email me when analysis completes".\n'
    )

    _send_in_thread(subject, body, owner.email, app)

# vim: ts=4 sw=4 et
