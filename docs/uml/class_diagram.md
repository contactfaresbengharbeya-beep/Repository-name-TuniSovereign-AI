# UML Class Diagram — Track 5 · Tunihara-MedGuard (Week 1)

Mermaid source (renders on GitHub / VS Code "Markdown Preview Mermaid Support"; export to PNG/SVG with
`npx @mermaid-js/mermaid-cli -i docs/uml/class_diagram.md -o docs/uml/class_diagram.svg` for the slides).
Class names, attributes and relationships mirror `backend/tunisovereign_core/models.py`; the test
`tests/test_uml_consistency.py` fails if they drift.

Starter → Track 5 mapping: `CooperativeUnit → HealthcareFacility`, `ResourceNodePlot → MedicationInventory`;
`Medication`, `Prescription`, `RedistributionRequest` are new; the other models keep their starter names.
All data is **synthetic** (`is_simulated`); patients exist only as irreversible pseudonyms.

```mermaid
classDiagram
  class StakeholderUser {
    +role: COOP_MEMBER|REGIONAL_AUDITOR|NATIONAL_ADMIN|AGENT_SERVICE
    +governorate: str
    +wallet_address: str
    +preferred_dialect: str
  }
  class HealthcareFacility {
    +code: str
    +name: str
    +facility_type: HOSPITAL|PCT_DEPOT|PHARMACY|CLINIC
    +governorate: str
    +latitude: float
    +longitude: float
    +daily_dispensing_capacity_units: float
    +track_domain: str
    +contract_address: str
    +is_simulated: bool
    +created_at: datetime
  }
  class Medication {
    +dci_name: str
    +atc_code: str
    +therapeutic_class: str
    +dosage_form: str
    +strength: str
    +unit_of_measure: str
    +is_chronic_care: bool
    +cnam_subsidy_status: str
    +requires_cold_chain: bool
    +is_simulated: bool
  }
  class MedicationInventory {
    +node_code: str
    +current_stock_units: float
    +reorder_threshold_units: float
    +safety_stock_days: int
    +last_reading_at: datetime
    +is_redistribution_hold: bool
    +is_simulated: bool
  }
  class TelemetryReading {
    +timestamp: datetime
    +metric_type: STOCK_LEVEL|DISPENSED_UNITS|CONSUMPTION_RATE|RX_COUNT|COLD_CHAIN_TEMP
    +value: float
    +ingestion_protocol: str
    +is_simulated: bool
    +signature_verified: bool
  }
  class Prescription {
    +issued_at: datetime
    +patient_pseudonym: hex64
    +quantity_units: float
    +cnam_coverage_category: str
    +eligibility_valid_until: date
    +is_simulated: bool
  }
  class AnomalyAndForecastEvent {
    +detected_at: datetime
    +anomaly_type: str
    +severity: LOW|MEDIUM|CRITICAL
    +isolation_forest_score: float
    +forecasted_stockout_days: float
    +details: json
    +is_simulated: bool
  }
  class RedistributionRequest {
    +units: float
    +status: PROPOSED|APPROVED|REJECTED|EXECUTED
    +rationale: json
    +is_simulated: bool
    +created_at: datetime
    +updated_at: datetime
  }
  class AgentLoopExecution {
    +thread_id: str
    +governorate: str
    +trigger_source: str
    +final_recommendation: str
    +confidence_score: float
    +created_at: datetime
  }
  class OnChainZKReceipt {
    +zk_commitment_hash: str
    +tx_hash: str
    +block_number: int
    +network_name: str
    +is_verified_on_chain: bool
  }

  HealthcareFacility "1" --> "*" MedicationInventory : holds
  Medication "1" --> "*" MedicationInventory : stocked as
  MedicationInventory "1" --> "*" TelemetryReading : emits
  MedicationInventory "1" --> "*" AnomalyAndForecastEvent : raises
  HealthcareFacility "1" --> "*" Prescription : dispenses
  Medication "1" --> "*" Prescription : prescribed
  Prescription "0..1" --> "*" AnomalyAndForecastEvent : related_prescription
  HealthcareFacility "0..1" --> "*" StakeholderUser : staff scope
  HealthcareFacility "1" --> "*" RedistributionRequest : source / destination
  Medication "1" --> "*" RedistributionRequest : concerns
  StakeholderUser "1" --> "*" RedistributionRequest : proposed_by / approved_by
  AnomalyAndForecastEvent "0..1" --> "*" RedistributionRequest : related_event
  AgentLoopExecution "1" --> "0..1" OnChainZKReceipt : anchors
```

## Notes

* **Isolation rule** (enforced in `scope.py` for REST, GraphQL and gRPC): `COOP_MEMBER` → own facility;
  `REGIONAL_AUDITOR` → own governorate; `NATIONAL_ADMIN` / `AGENT_SERVICE` → everything.
* `TelemetryReading` is unique on `(inventory, metric_type, timestamp)` → ingestion is idempotent.
* `MedicationInventory.current_stock_units` / `last_reading_at` are derived from validated telemetry,
  never client-writable. Consumption is derived from `DISPENSED_UNITS` telemetry (no separate table in Week 1).
* `AgentLoopExecution` and `OnChainZKReceipt` exist as schema only; Week 3 populates them.
