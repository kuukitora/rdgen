from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from rdgenerator.updates import UPDATE_CHANNELS, publish_release


class Command(BaseCommand):
    help = "Sign and publish a Windows MSI release to one configured RDGen update channel."

    def add_arguments(self, parser):
        parser.add_argument("channel", choices=UPDATE_CHANNELS)
        parser.add_argument("version")
        parser.add_argument("msi", type=Path)
        parser.add_argument("--signing-key", type=Path, required=True)

    def handle(self, *args, channel, version, msi, signing_key, **options):
        try:
            release = publish_release(channel, version, msi, signing_key)
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Published {channel} {release}."))
