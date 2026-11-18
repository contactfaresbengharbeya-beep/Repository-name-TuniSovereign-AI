"""
manage.py seed_tunihara — loads SYNTHETIC Track 5 data (facilities, medications, inventory positions,
demo users for the four roles, pseudonymised prescriptions, sample redistribution requests). Idempotent.
"""
import hashlib
import hmac
import random
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from backend.tunisovereign_core import seed_data
from backend.tunisovereign_core.models import (
    HealthcareFacility,
    Medication,
    MedicationInventory,
    Prescription,
    RedistributionRequest,
    StakeholderUser,
)

import os

DEMO_USERS = [
    # username, role, governorate, facility_code
    ("coop_kasserine", "COOP_MEMBER", "Kasserine", "KAS-PHA-01"),
    ("coop_tunis", "COOP_MEMBER", "Tunis", "TUN-HOP-01"),
    ("auditor_kasserine", "REGIONAL_AUDITOR", "Kasserine", None),
    ("auditor_sfax", "REGIONAL_AUDITOR", "Sfax", None),
    ("national_admin", "NATIONAL_ADMIN", "Tunis", None),
    ("agent_service", "AGENT_SERVICE", "Tunis", None),
]


def pseudonym(index: int) -> str:
    """Irreversible synthetic patient pseudonym (HMAC digest of a counter — no real identity exists)."""
    return hmac.new(
        settings.TELEMETRY_HMAC_SECRET.encode(), f"SIM-PATIENT-{index}".encode(), hashlib.sha256
    ).hexdigest()


class Command(BaseCommand):
    help = "Seed synthetic Tunihara-MedGuard data (facilities, medications, inventory, users, prescriptions)."

    def add_arguments(self, parser):
        parser.add_argument("--prescriptions", type=int, default=300, help="synthetic prescriptions to create")
        parser.add_argument("--seed", type=int, default=2026, help="random seed for prescriptions")
        parser.add_argument("--reset-passwords", action="store_true", help="reset demo user passwords")

    @transaction.atomic
    def handle(self, *args, **opts):
        password = os.environ.get("SEED_DEMO_PASSWORD", "").strip()
        generated = False
        if not password:
            password, generated = secrets.token_urlsafe(14), True

        facilities = {}
        for code, name, ftype, gov, lat, lon in seed_data.FACILITIES:
            fac, _ = HealthcareFacility.objects.update_or_create(
                code=code,
                defaults=dict(name=name, facility_type=ftype, governorate=gov, latitude=lat, longitude=lon,
                              is_simulated=True),
            )
            facilities[code] = fac

        medications = {}
        for dci, atc, tclass, form, strength, unit, cold, _rel in seed_data.MEDICATIONS:
            med, _ = Medication.objects.update_or_create(
                atc_code=atc,
                defaults=dict(
                    dci_name=dci, therapeutic_class=tclass, dosage_form=form, strength=strength,
                    unit_of_measure=unit, requires_cold_chain=cold, is_chronic_care=True,
                    cnam_subsidy_status=Medication.SubsidyStatus.ILLUSTRATIVE_SUBSIDISED, is_simulated=True,
                ),
            )
            medications[atc] = med

        for node in seed_data.build_nodes():
            MedicationInventory.objects.update_or_create(
                node_code=node["node_code"],
                defaults=dict(
                    facility=facilities[node["facility_code"]],
                    medication=medications[node["atc_code"]],
                    reorder_threshold_units=node["reorder_threshold"],
                    safety_stock_days=seed_data.SAFETY_STOCK_DAYS,
                    is_simulated=True,
                ),
            )
        # initial stock only where no telemetry has set it yet
        for node in seed_data.build_nodes():
            MedicationInventory.objects.filter(node_code=node["node_code"], last_reading_at__isnull=True).update(
                current_stock_units=node["initial_stock"]
            )

        for username, role, gov, fac_code in DEMO_USERS:
            user, created = StakeholderUser.objects.get_or_create(
                username=username,
                defaults=dict(role=role, governorate=gov, facility=facilities.get(fac_code) if fac_code else None,
                              is_staff=(role == "NATIONAL_ADMIN")),
            )
            if created or opts["reset_passwords"]:
                user.set_password(password)
                user.save(update_fields=["password"])

        n_rx = 0
        if opts["prescriptions"] and not Prescription.objects.exists():
            n_rx = self._seed_prescriptions(opts["prescriptions"], opts["seed"], facilities, medications)
        self._seed_redistribution(facilities, medications)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(facilities)} facilities, {len(medications)} medications, "
            f"{MedicationInventory.objects.count()} inventory positions, {len(DEMO_USERS)} demo users, "
            f"{n_rx} new prescriptions (ALL SYNTHETIC)."
        ))
        if generated:
            self.stdout.write(f"SEED_DEMO_PASSWORD was not set; demo users were given this password: {password}")

    def _seed_prescriptions(self, count, seed, facilities, medications):
        rng = random.Random(seed)
        now = timezone.now()
        pharm = [f for f in facilities.values() if f.facility_type in ("PHARMACY", "CLINIC", "HOSPITAL")]
        meds = list(medications.values())
        rows = []
        for i in range(count):
            fac, med = rng.choice(pharm), rng.choice(meds)
            issued = now - timedelta(hours=rng.randint(1, 24 * 30))
            pid = pseudonym(i % max(1, count // 3))  # patients refill: ~3 prescriptions per pseudonym
            rows.append(Prescription(
                facility=fac, medication=med, issued_at=issued, patient_pseudonym=pid,
                quantity_units=float(rng.choice([1, 1, 2, 3])),
                cnam_coverage_category=rng.choice(["CHRONIC_FULL", "CHRONIC_PARTIAL"]),
                eligibility_valid_until=(now + timedelta(days=rng.randint(30, 365))).date(),
                is_simulated=True,
            ))
            if rng.random() < 0.05:  # injected duplicate: same patient+medication at another facility within 3 days
                other = rng.choice([f for f in pharm if f != fac])
                rows.append(Prescription(
                    facility=other, medication=med, issued_at=issued + timedelta(days=rng.randint(0, 3)),
                    patient_pseudonym=pid, quantity_units=1.0, cnam_coverage_category="CHRONIC_FULL",
                    eligibility_valid_until=(now + timedelta(days=90)).date(), is_simulated=True,
                ))
        Prescription.objects.bulk_create(rows)
        return len(rows)

    def _seed_redistribution(self, facilities, medications):
        if RedistributionRequest.objects.exists():
            return
        admin = StakeholderUser.objects.filter(username="national_admin").first()
        samples = [
            ("KAS-HOP-01", "KAS-PHA-01", "A10AE04", 20.0, "Illustrative: simulated low insulin cover"),
            ("TUN-PCT-01", "SFA-HOP-01", "A10BA02", 300.0, "Illustrative: simulated regional rebalancing"),
        ]
        for src, dst, atc, units, why in samples:
            RedistributionRequest.objects.create(
                source_facility=facilities[src], destination_facility=facilities[dst], medication=medications[atc],
                units=units, proposed_by=admin, rationale={"note": why, "synthetic": True}, is_simulated=True,
            )
