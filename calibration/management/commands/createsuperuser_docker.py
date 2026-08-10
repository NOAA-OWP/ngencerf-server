from django.contrib.auth.management.commands import createsuperuser
from django.contrib.auth import get_user_model
from django.core.management import CommandError


class Command(createsuperuser.Command):
    help = "Create a superuser, and allow password to be provided"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--password",
            dest="password",
            default=None,
            help="Specifies the password for the superuser.",
        )

    def handle(self, *args, **options):
        # TODO Delete me
        print("This command is deprecated and can be deleted")
        password = options.get("password")
        email = options.get("email")

        if password and not email:
            raise CommandError("--email is required if specifying --password")
        
        User = get_user_model()
        if User.objects.filter(email=email).exists():
            self.stdout.write(self.style.WARNING(f"Superuser account [{email}] already exists."))
        else:
            if not password:
                raise CommandError("--password is required")

            User.objects.create_superuser(
                email=email,
                password=password,
            )
            self.stdout.write(self.style.SUCCESS(f"Superuser account [{email}] created successfully."))
