import base64
import smtplib
from geventconnpool import ConnectionPool

from inbox.server.log import get_logger
from inbox.server.basicauth import AUTH_TYPES
from inbox.server.auth.base import verify_imap_account
from inbox.server.models import session_scope
from inbox.server.models.tables.base import Account

SMTP_HOSTS = {'Gmail': 'smtp.gmail.com'}
SMTP_PORT = 587

DEFAULT_POOL_SIZE = 5

# Memory cache for per-user SMTP connection pool.
account_id_to_connection_pool = {}

# TODO[k]: Other types (LOGIN, XOAUTH, PLAIN-CLIENTTOKEN, CRAM-MD5)
AUTH_EXTNS = {'OAuth': 'XOAUTH2',
              'Password': 'PLAIN'}


class SendMailError(Exception):
    pass


def get_connection_pool(account_id, pool_size=None):
    pool_size = pool_size or DEFAULT_POOL_SIZE

    if account_id_to_connection_pool.get(account_id) is None:
        account_id_to_connection_pool[account_id] = \
            SMTPConnectionPool(account_id, num_connections=pool_size)

    return account_id_to_connection_pool[account_id]


class SMTPConnectionPool(ConnectionPool):
    def __init__(self, account_id, num_connections=5, debug=False):
        self.log = get_logger(account_id, 'sendmail: connection_pool')
        self.log.info('Creating SMTP connection pool for account {0} with {1} '
                      'connections'.format(account_id, num_connections))

        self.account_id = account_id
        self._set_account_info()

        self.debug = debug

        self.auth_handlers = {'OAuth': self.smtp_oauth,
                              'Password': self.smtp_password}

        # 1200s == 20min
        ConnectionPool.__init__(self, num_connections, keepalive=1200)

    def _set_account_info(self):
        with session_scope() as db_session:
            account = db_session.query(Account).get(self.account_id)

            #self.full_name = account.full_name
            self.full_name = 'TEST'
            self.email_address = account.email_address
            self.provider = account.provider

            self.auth_type = AUTH_TYPES.get(account.provider)

            if self.auth_type == 'OAuth':
                # Refresh OAuth token if need be
                account = verify_imap_account(db_session, account)
                self.o_access_token = account.o_access_token
            else:
                assert self.auth_type == 'Password'
                self.password = account.password

    def _new_connection(self):
        try:
            connection = smtplib.SMTP(SMTP_HOSTS[self.provider], SMTP_PORT)
        except smtplib.SMTPConnectError as e:
            self.log.error('SMTPConnectError')
            raise e

        connection.set_debuglevel(self.debug)

        # Put the SMTP connection in TLS mode
        connection.ehlo()

        if not connection.has_extn('starttls'):
            raise SendMailError('Required SMTP STARTTLS not supported.')

        connection.starttls()
        connection.ehlo()

        # Auth the connection
        authed_connection = self.auth_connection(connection)

        return authed_connection

    def _keepalive(self, c):
        c.noop()

    def auth_connection(self, c):
        # Auth mechanisms supported by the server
        if not c.has_extn('auth'):
            raise SendMailError('Required SMTP AUTH not supported.')

        supported_types = c.esmtp_features['auth'].strip().split()

        # Auth mechanism needed for this account
        if AUTH_EXTNS.get(self.auth_type) not in supported_types:
            raise SendMailError('Required SMTP Auth mechanism not supported.')

        auth_handler = self.auth_handlers.get(self.auth_type)
        return auth_handler(c)

    # OAuth2 authentication
    def smtp_oauth(self, c):
        try:
            auth_string = 'user={0}\1auth=Bearer {1}\1\1'.\
                format(self.email_address, self.o_access_token)
            c.docmd('AUTH', 'XOAUTH2 {0}'.format(
                base64.b64encode(auth_string)))
        except smtplib.SMTPAuthenticationError as e:
            self.log.error('SMTP Auth failed for: {0}'.format(
                self.email_address))
            raise e

        self.log.info('SMTP Auth success for: {0}'.format(self.email_address))
        return c

    # Password authentication
    def smtp_password(self, c):
        # TODO[k]
        raise NotImplementedError


class SMTPClient(object):
    def __init__(self, account_id):
        self.account_id = account_id
        self.pool = get_connection_pool(self.account_id)
        # Required for Gmail
        self.full_name = self.pool.full_name
        self.email_address = self.pool.email_address

        self.log = get_logger(account_id, 'sendmail')

        # TODO[k]
        # self.connection.quit()

    def _send(self, recipients, msg):
        with self.pool.get() as c:
            try:
                failures = c.sendmail(self.email_address, recipients, msg)
            # Sent to none successfully
            except smtplib.SMTPException as e:
                self.log.error('Sending failed: Exception {0}'.format(e))
                # TODO[k]: Retry
                raise

            # Sent to all successfully
            if not failures:
                self.log.info('Sending successful: {0} to {1}'.format(
                    self.email_address, ', '.join(recipients)))
                return True

            # Sent to atleast one successfully
            # TODO[k]: Handle this!
            for r, e in failures.iteritems():
                self.log.error('Send failed: {0} to {1}, code: {2}'.format(
                    self.email_address, r, e[0]))
                return False

    def send_mail(self, recipients, subject, body, attachments=None):
        raise NotImplementedError
