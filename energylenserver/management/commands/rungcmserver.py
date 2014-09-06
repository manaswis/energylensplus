"""
Main XMPP CCS Client file

Usage: start the XMPP client on a different terminal along with
Django server running simultaneously

"""
import sys
from multiprocessing.managers import BaseManager

# Django imports
from django.core.management.base import BaseCommand, CommandError
from energylenserver.gcmxmppclient.msgclient import MessageClient


class ClientManager(BaseManager):
    pass


class Command(BaseCommand):
    help = "Establishes a persistent connection with Google's Server via XMPP"

    def handle(self, *args, **options):
        # Creates a message client that is connected to the Google server
        msg_client = MessageClient()
        msg_client.register_handlers()
        client = msg_client.get_connection_client()

        ClientManager.register('get_client', callable=lambda: msg_client)
        manager = ClientManager(address=('', 50000), authkey='abracadabra')
        server = manager.get_server()
        server.serve_forever()

        try:
            while True:
                client.Process(1)
                # Restore connection if connection broken
                if not client.isConnected():
                    if not msg_client.connect_to_gcm_server():
                        self.stdout.write("Authentication failed! Try again!")
                        sys.exit(1)
        except KeyboardInterrupt:
            self.stdout.write("\n\nInterrupted by user, shutting down..")
            sys.exit(0)