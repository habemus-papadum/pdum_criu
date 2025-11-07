"""Example tests for criu."""

from pdum import criu


def test_version():
    """Test that the package has a version."""
    assert hasattr(criu, "__version__")
    assert isinstance(criu.__version__, str)
    assert len(criu.__version__) > 0


def test_import():
    """Test that the package can be imported."""
    assert criu is not None


