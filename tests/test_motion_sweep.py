from astra_blender import motion


def box(x, y, z, size=1.0):
    return [[x, y, z], [x + size, y + size, z + size]]


def test_sweep_summary_flags_new_overlaps_ground_breaches_and_detachment():
    frames = [1, 12, 24]
    sweep = {
        "frames": frames,
        "bounds": {
            # Broad flat floor: 20 x 20, 0.05 high.
            "Ground": [[[-10, -10, 0], [10, 10, 0.05]]] * 3,
            # A stands still; B slides into it by the last frame.
            "A": [box(0, 0, 0.05)] * 3,
            "B": [box(3, 0, 0.05), box(1.5, 0, 0.05), box(0.5, 0, 0.05)],
            # C sinks through the floor mid-motion and comes back.
            "C": [box(5, 5, 0.05), box(5, 5, -0.6), box(5, 5, 0.05)],
            # D is an assembled part that drifts away from its anchor A.
            "D": [box(1.05, -1.05, 0.05), box(1.05, -1.5, 0.05), box(1.05, -2.2, 0.05)],
            # E and F overlap from the start: nested by design, not a finding.
            "E": [box(-5, -5, 0.05, 2)] * 3,
            "F": [box(-4.5, -4.5, 0.5, 0.5)] * 3,
        },
        "anchors": {"D": "A"},
        "max_gaps": {"D": 0.15},
    }
    summary = motion.sweep_summary(sweep)
    assert summary["frames"] == frames
    assert summary["new_overlaps"] == [{"objects": ["A", "B"], "first_frame": 24}]
    assert summary["below_ground"] == [{"object": "C", "ground": "Ground", "frames": [12]}]
    assert len(summary["detached"]) == 1
    detached = summary["detached"][0]
    assert detached["objects"] == ["D", "A"] and detached["frame"] == 24 and detached["max_gap"] > 1
    assert "not proof" in summary["limits"]


def test_empty_sweep_and_report_folding():
    assert motion.sweep_summary({"frames": [], "bounds": {}})["new_overlaps"] == []
    data = {
        "start": 1,
        "end": 24,
        "actions": [],
        "samples": [],
        "sweep": {"frames": [1], "bounds": {"A": [box(0, 0, 0)]}},
    }
    folded = motion.report(data, lambda scene: scene)
    # Raw boxes never reach the model; the summary replaces them.
    assert "bounds" not in folded["sweep"]
    assert folded["sweep"]["new_overlaps"] == []
