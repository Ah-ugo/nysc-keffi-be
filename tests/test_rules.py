from app.services.rules import *


def test_csv_formula_injection():
    assert csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert csv_safe("+1") == "'+1" and csv_safe("@x") == "'@x" and csv_safe("ok") == "ok"


def test_callup():
    assert normalise_callup(" kg/26c/1234 ") == "KG/26C/1234"
    assert valid_callup("KG/26C/1234", r"^[A-Z0-9/\-]{6,25}$") and not valid_callup("x", r"^[A-Z0-9/\-]{6,25}$")


CFG = {"lat": 8.85, "lon": 7.87, "radius_m": 500, "max_accuracy_m": 100}


def test_location():
    assert evaluate_location(8.85, 7.87, 20, CFG).result == "passed"
    assert evaluate_location(9.5, 7.87, 20, CFG).result == "failed"
    assert evaluate_location(8.85, 7.87, 500, CFG).result == "review"
    assert evaluate_location(None, None, None, CFG).result == "review"


def test_face_decision_and_order():
    assert decide_face(95, True, None, 90, 75)[0] == "passed"
    assert decide_face(80, True, None, 90, 75)[0] == "review"
    assert decide_face(40, True, None, 90, 75)[0] == "failed"
    assert decide_face(99, False, None, 90, 75)[0] == "review"
    assert decide_face(None, True, None, 90, 75)[0] == "review"
    # a failed/unresolved face can never end in verified_present, whatever location says
    assert final_status("failed", "passed") == "face_failed"
    assert final_status("review", "passed") == "pending_manual_review"
    assert final_status("passed", "failed") == "location_failed"
    assert final_status("passed", "passed") == "verified_present"
