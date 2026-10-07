import unittest
from dataclasses import fields

from agent_bridge.binding_control import (
    BindingControlState,
    BindingGenerationRecord,
    BindingLineage,
    BindingRef,
)


def lineage(binding_id="binding-a", agent_id="a", *generations):
    values = generations or (3,)
    return BindingLineage(
        binding_id,
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
        with self.assertRaisesRegex(ValueError, "agent must match"):
            BindingControlState(
                lineages=[lineage()],
                active_by_room_agent=[
                    (("other-room", "other"), BindingRef("binding-a", 3))
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
                    lineage("binding-a", "a", 3),
                    lineage("binding-a", "b", 4),
                ]
            )

    def test_generations_are_unique_and_strictly_increasing(self):
        for values in ((4, 4), (9, 4)):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "unique and increasing"):
                    lineage("binding-a", "a", *values)

    def test_historical_generations_survive_active_pointer_advance(self):
        value = BindingControlState(
            lineages=[lineage("binding-a", "a", 4, 9)],
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
            lineages=[lineage("binding-a", "a", 4, 9)],
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
            {"binding_id", "agent_id", "generations"},
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

    def test_detached_lineage_can_attach_detach_and_move_without_identity_change(self):
        value = BindingControlState(lineages=[lineage("binding-a", "a", 3)])
        ref = BindingRef("binding-a", 3)

        value.attach(("room-a", "a"), ref)
        self.assertEqual(value.detach(("room-a", "a")), ref)
        self.assertFalse(value.snapshot().active_by_room_agent)

        value.attach(("room-a", "a"), ref)
        self.assertEqual(value.move(("room-a", "a"), ("room-b", "a")), ref)
        self.assertEqual(value.snapshot().active_by_room_agent, {("room-b", "a"): ref})

    def test_same_binding_cannot_be_active_in_two_rooms_or_overwrite_target(self):
        ref = BindingRef("binding-a", 3)
        value = BindingControlState(
            lineages=[lineage("binding-a", "a", 3)],
            active_by_room_agent=[(("room-a", "a"), ref)],
        )
        with self.assertRaisesRegex(ValueError, "only one room"):
            value.attach(("room-b", "a"), ref)

        other = BindingLineage("binding-b", "a", (BindingGenerationRecord(1),))
        value.register_lineage(other)
        value.attach(("room-b", "a"), BindingRef("binding-b", 1))
        with self.assertRaisesRegex(ValueError, "target"):
            value.move(("room-a", "a"), ("room-b", "a"))


if __name__ == "__main__":
    unittest.main()
