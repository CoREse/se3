"""``StepTypeValue`` must mirror ``StepType`` exactly.

The schema enum is the validation face of engine.json. Any step type the model
layer can serialize but the schema does not know is a persisted flow that fails
validation for no reason the operator can see — the enum silently under-reports
the engine's step-type space. This test is the mechanical guard that a new
``StepType`` member cannot be added without extending the mirror.
"""

from __future__ import annotations

from tianluo.engine.models import StepType
from tianluo.engine.schema import StepTypeValue


def test_schema_mirrors_every_serializable_step_type():
    model_values = {s.value for s in StepType}
    schema_values = {s.value for s in StepTypeValue}

    assert model_values - schema_values == set(), (
        "StepType members missing from schema.StepTypeValue — persisted flows "
        "containing them would be rejected by the schema"
    )
    assert schema_values - model_values == set(), (
        "schema.StepTypeValue members with no StepType counterpart"
    )


def test_investigate_is_present_in_both():
    assert StepType.INVESTIGATE.value == "investigate"
    assert StepTypeValue.INVESTIGATE.value == "investigate"
