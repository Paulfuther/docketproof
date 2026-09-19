from django.core.management.base import BaseCommand

from arl.quiz.importers import (
    DEFAULT_WORKPLACE_INSPECTION_JSON,
    import_checklist_template,
    load_checklist_payload,
)


class Command(BaseCommand):
    help = (
        "Import a checklist template from ENMCDS840-style JSON "
        "(default: Petro Canada Workplace Inspection BC & ON)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "json_path",
            nargs="?",
            default=str(DEFAULT_WORKPLACE_INSPECTION_JSON),
            help="Path to checklist JSON (defaults to bundled ENMCDS840-6.3 file)",
        )

    def handle(self, *args, **options):
        payload = load_checklist_payload(options["json_path"])
        template = import_checklist_template(payload)
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported '{template.name}' "
                f"({template.document_id or 'no document_id'}) "
                f"with {template.items.count()} items."
            )
        )
