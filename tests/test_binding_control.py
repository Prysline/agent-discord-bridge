import unittest
from dataclasses import fields

from agent_bridge.binding_control import (
    BindingControlState,
    BindingGenerationRecord,
    BindingLineage,
    BindingRef,
)


def lineage(binding_id="binding-a", room_id="room", agent_id="a", *generations):
    values = generations or (3,)
    return BindingLineage(
        binding_id,
        room_id,
        agent_id,
        tuple(BindingGenerationRecord(value) for value in values),
    )


class BindingControlStateTests(unittest.TestCase):
    def test_room_agent_has_at_most_one_active_binding(self):
        with self.assertRaisesRegex(ValueError, "only one active binding"):
            BindingControlState(
                lineages=[lineage()],
                active_by_room_agent=[
                    (("room", "a"), BindingRef("binding-a", 3)),
                    (("room", "a"), BindingRef("binding-a", 3)),
                ],
            )

    def test_active_ref_requires_existing_lineage(self):
        with self.assertRaisesRegex(ValueError, "existing lineage"):
            BindingControlState(
                active_by_room_agent=[
                    (("room", "a"), BindingRef("missing", 3))
                ]
            )

    def test_active_ref_requires_matching_immutable_owner(self):
        with self.assertRaisesRegex(ValueError, "owner must match"):
            BindingControlState(
                lineages=[lineage()],
                active_by_room_agent=[
                    (("other-room", "a"), BindingRef("binding-a", 3))
                ],
            )

    def test_active_ref_requires_existing_generation(self):
        with self.assertRaisesRegex(ValueError, "existing generation"):
            BindingControlState(
                lineages=[lineage()],
                active_by_room_agent=[
                    (("room", "a"), BindingRef("binding-a", 8))
                ],
            )

    def test_binding_id_cannot_describe_multiple_lineages(self):
        with self.assertRaisesRegex(ValueError, "exactly one lineage"):
            BindingControlState(
                lineages=[
                    lineage("binding-a", "room", "a", 3),
                    lineage("binding-a", "other-room", "b", 4),
                ]
            )

    def test_generations_are_unique_and_strictly_increasing(self):
        for values in ((4, 4), (9, 4)):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "unique and increasing"):
                    lineage("binding-a", "room", "a", *values)

    def test_historical_generations_survive_active_pointer_advance(self):
        value = BindingControlState(
            lineages=[lineage("binding-a", "room", "a", 4, 9)],
            active_by_room_agent=[
                (("room", "a"), BindingRef("binding-a", 9))
            ],
        )

        snapshot = value.snapshot()
        self.assertEqual(
            tuple(
                item.generation
                for item in snapshot.bindings_by_id["binding-a"].generations
            ),
            (4, 9),
        )

    def test_higher_generation_does_not_become_active_implicitly(self):
        value = BindingControlState(
            lineages=[lineage("binding-a", "room", "a", 4, 9)],
            active_by_room_agent=[
                (("room", "a"), BindingRef("binding-a", 4))
            ],
        )

        self.assertEqual(
            value.snapshot().active_by_room_agent[("room", "a")].generation,
            4,
        )

    def test_shared_types_contain_no_native_session_reference(self):
        self.assertEqual(
            {item.name for item in fields(BindingRef)},
            {"binding_id", "generation"},
        )
        self.assertEqual(
            {item.name for item in fields(BindingGenerationRecord)},
            {"generation"},
        )
        self.assertEqual(
            {item.name for item in fields(BindingLineage)},
            {"binding_id", "room_id", "agent_id", "generations"},
        )

    def test_snapshot_is_detached_from_live_state(self):
        value = BindingControlState(
            lineages=[lineage()],
            active_by_room_agent=[
                (("room", "a"), BindingRef("binding-a", 3))
            ],
        )

        snapshot = value.snapshot()
        snapshot.active_by_room_agent.clear()
        snapshot.bindings_by_id.clear()

        live = value.snapshot()
        self.assertEqual(
            live.active_by_room_agent[("room", "a")],
            BindingRef("binding-a", 3),
        )
        self.assertIn("binding-a", live.bindings_by_id)


if __name__ == "__main__":
    unittest.main()
