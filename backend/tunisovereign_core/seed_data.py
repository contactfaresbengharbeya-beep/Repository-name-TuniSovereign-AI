"""
SYNTHETIC catalogue shared by ``manage.py seed_tunihara`` and the telemetry simulator (pure Python).

Everything here is invented for the academic project: facility names carry "(simulé)", demand figures are
illustrative, and subsidy statuses are labelled ILLUSTRATIVE. Medication names/ATC codes are public
pharmacological classifications, not CNAM or PCT data.
"""

# code, name, type, governorate, latitude, longitude
FACILITIES = [
    ("TUN-HOP-01", "Hôpital universitaire (simulé) – Tunis", "HOSPITAL", "Tunis", 36.80, 10.18),
    ("TUN-PCT-01", "Dépôt régional PCT (simulé) – Tunis", "PCT_DEPOT", "Tunis", 36.77, 10.12),
    ("ARI-PHA-01", "Pharmacie communautaire (simulée) – Ariana", "PHARMACY", "Ariana", 36.86, 10.19),
    ("SFA-HOP-01", "Hôpital régional (simulé) – Sfax", "HOSPITAL", "Sfax", 34.74, 10.76),
    ("SOU-PHA-01", "Pharmacie communautaire (simulée) – Sousse", "PHARMACY", "Sousse", 35.83, 10.64),
    ("KAI-HOP-01", "Hôpital régional (simulé) – Kairouan", "HOSPITAL", "Kairouan", 35.68, 10.10),
    ("KAS-HOP-01", "Hôpital régional (simulé) – Kasserine", "HOSPITAL", "Kasserine", 35.17, 8.83),
    ("KAS-PHA-01", "Pharmacie communautaire (simulée) – Sbeitla, Kasserine", "PHARMACY", "Kasserine", 35.23, 9.12),
    ("SBZ-PHA-01", "Pharmacie communautaire (simulée) – Regueb, Sidi Bouzid", "PHARMACY", "Sidi_Bouzid", 34.86, 9.78),
    ("GAF-HOP-01", "Hôpital régional (simulé) – Gafsa", "HOSPITAL", "Gafsa", 34.43, 8.78),
    ("MED-PHA-01", "Pharmacie communautaire (simulée) – Médenine", "PHARMACY", "Medenine", 33.35, 10.50),
    ("TOZ-CLI-01", "Centre de santé de base (simulé) – Tozeur", "CLINIC", "Tozeur", 33.92, 8.13),
]

# dci_name, atc_code, therapeutic_class, dosage_form, strength, unit, requires_cold_chain, relative_demand
MEDICATIONS = [
    ("Metformin", "A10BA02", "DIABETES", "tablet", "850 mg", "boxes", False, 1.0),
    ("Insulin glargine", "A10AE04", "DIABETES", "injection pen", "100 IU/mL", "pens", True, 0.4),
    ("Amlodipine", "C08CA01", "HYPERTENSION", "tablet", "5 mg", "boxes", False, 0.9),
    ("Enalapril", "C09AA02", "HYPERTENSION", "tablet", "10 mg", "boxes", False, 0.7),
    ("Salbutamol", "R03AC02", "ASTHMA_COPD", "inhaler", "100 µg/dose", "inhalers", False, 0.5),
    ("Levothyroxine", "H03AA01", "THYROID", "tablet", "100 µg", "boxes", False, 0.6),
    ("Atorvastatin", "C10AA05", "CARDIOVASCULAR", "tablet", "20 mg", "boxes", False, 0.8),
]

# Illustrative daily demand (units/day) per facility type, multiplied by the medication's relative demand.
BASE_DAILY_DEMAND = {"HOSPITAL": 120.0, "PCT_DEPOT": 400.0, "PHARMACY": 35.0, "CLINIC": 15.0}
# Medications stocked per facility type (ATC codes).
STOCKED = {
    "HOSPITAL": [m[1] for m in MEDICATIONS],
    "PCT_DEPOT": [m[1] for m in MEDICATIONS],
    "PHARMACY": ["A10BA02", "A10AE04", "C08CA01", "C09AA02", "R03AC02"],
    "CLINIC": ["A10BA02", "C08CA01", "C09AA02", "R03AC02"],
}
SAFETY_STOCK_DAYS = 7
INITIAL_COVER_DAYS = 20


def node_code_for(facility_code: str, atc_code: str) -> str:
    return f"{facility_code}-{atc_code}"


def build_nodes() -> list:
    """Every monitored (facility x medication) position with the parameters the simulator needs."""
    by_atc = {m[1]: m for m in MEDICATIONS}
    nodes = []
    for code, name, ftype, gov, lat, lon in FACILITIES:
        for atc in STOCKED[ftype]:
            med = by_atc[atc]
            daily = round(BASE_DAILY_DEMAND[ftype] * med[7], 2)
            nodes.append(
                {
                    "node_code": node_code_for(code, atc),
                    "facility_code": code,
                    "facility_type": ftype,
                    "governorate": gov,
                    "atc_code": atc,
                    "dci_name": med[0],
                    "unit": med[5],
                    "requires_cold_chain": med[6],
                    "daily_demand": daily,
                    "reorder_threshold": round(daily * SAFETY_STOCK_DAYS, 2),
                    "initial_stock": round(daily * INITIAL_COVER_DAYS, 2),
                }
            )
    return nodes
