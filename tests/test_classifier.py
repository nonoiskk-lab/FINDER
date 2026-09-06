"""IT classification, including the false positives GeM is full of."""

from __future__ import annotations

import pytest
from tests.conftest import make_tender

from gem_intel.analyze.classifier import ItClassifier
from gem_intel.models import Confidence, TenderKind


@pytest.fixture
def classifier(settings):
    return ItClassifier(settings)


@pytest.mark.parametrize("title,category", [
    ("Supply of Laptop for office use", "Laptop - Business"),
    ("Procurement of Desktop Computer with Monitor", "Desktop Computer"),
    ("Supply of Multifunction Printer (MFP)", "MFP"),
    ("Supply and installation of CCTV Surveillance System", "CCTV"),
    ("Supply of Online UPS 5 KVA", "UPS"),
    ("Supply of Network Switch and Router", "Networking"),
    ("Supply of SSD and External Hard Disk", "Storage"),
])
def test_it_products_are_detected(classifier, title, category):
    result = classifier.classify(make_tender(title=title, category=category,
                                             item_description=title))
    assert result.is_it_related, title
    assert result.confidence is Confidence.CONFIRMED


@pytest.mark.parametrize("title,category", [
    ("Annual Maintenance Contract of Computers and Printers", "AMC Services"),
    ("Repair and maintenance of computer hardware", "Computer Maintenance"),
    ("Networking and LAN installation work", "Networking Services"),
    ("Hiring of IT support services", "IT Support"),
])
def test_it_services_are_detected(classifier, title, category):
    result = classifier.classify(make_tender(title=title, category=category,
                                             item_description=title))
    assert result.is_it_related, title


@pytest.mark.parametrize("title,category", [
    ("AMC of Lift and Elevator", "Lift Maintenance"),
    ("AMC of Air Conditioner units", "HVAC AMC"),
    ("Printing of books and binding work", "Printing Services"),
    ("Supply of Office Furniture including Computer Table", "Office Furniture"),
    ("Hiring of security guard services", "Manpower"),
    ("Supply of patient monitor for ICU", "Medical Equipment"),
])
def test_lookalikes_are_rejected(classifier, title, category):
    """The classifier's real job is rejecting these, not finding laptops."""
    result = classifier.classify(make_tender(title=title, category=category,
                                             item_description=title))
    assert not result.is_it_related, f"should have been vetoed: {title}"


def test_supply_is_distinguished_from_service(classifier):
    supply = classifier.classify(make_tender(
        title="Supply of Laptop", item_description="Supply of Laptop", category="Laptop"))
    service = classifier.classify(make_tender(
        title="Annual Maintenance Contract of Computers",
        item_description="AMC of computers", category="AMC Services"))
    assert supply.kind is TenderKind.PRODUCT_SUPPLY
    assert service.kind is TenderKind.SERVICE


def test_supply_and_installation_is_both(classifier):
    result = classifier.classify(make_tender(
        title="Supply, Installation and Commissioning of CCTV System",
        item_description="Supply installation and maintenance of CCTV",
        category="CCTV"))
    assert result.kind is TenderKind.SUPPLY_AND_SERVICE


def test_mixed_bid_is_not_vetoed_by_one_negative_signal(classifier):
    """'Laptops and computer tables' is still a laptop bid."""
    result = classifier.classify(make_tender(
        title="Supply of 50 Laptops and 10 Computer Tables",
        item_description="Supply of laptop, notebook computer and computer table",
        category="Laptop"))
    assert result.is_it_related


def test_signal_found_only_in_documents_is_inferred_not_confirmed(classifier):
    result = classifier.classify(
        make_tender(title="Supply of equipment", category="Miscellaneous",
                    item_description="Supply of equipment"),
        document_text="The equipment comprises 40 desktop computers with monitor.",
    )
    assert result.is_it_related
    assert result.confidence is Confidence.INFERRED


def test_reason_is_always_populated(classifier):
    for title in ("Supply of Laptop", "AMC of Lift"):
        result = classifier.classify(make_tender(title=title, item_description=title))
        assert result.reason
