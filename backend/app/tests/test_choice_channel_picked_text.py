from app.services.choice_channel import picked_text


def test_picked_text_includes_the_action_outcome_message() -> None:
    assert picked_text({
        "picked": {"label": "Use backing panel"},
        "outcome": {"message": "A dark panel was applied; the build is resuming."},
    }) == "Picked: Use backing panel. A dark panel was applied; the build is resuming."
