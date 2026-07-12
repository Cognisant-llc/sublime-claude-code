from claudeide.pathurl import path_to_uri, uri_to_path


def test_windows_path_roundtrip():
    p = "C:\\Users\\a0dro\\proj\\main.py"
    uri = path_to_uri(p)
    assert uri == "file:///C:/Users/a0dro/proj/main.py"
    assert uri_to_path(uri) == p


def test_windows_path_with_spaces():
    p = "C:\\My Docs\\hello world.py"
    uri = path_to_uri(p)
    assert uri == "file:///C:/My%20Docs/hello%20world.py"
    assert uri_to_path(uri) == p


def test_windows_forward_slashes_normalized():
    assert path_to_uri("C:/proj/x.py") == "file:///C:/proj/x.py"


def test_drive_letter_uppercased():
    assert path_to_uri("c:\\proj\\x.py") == "file:///C:/proj/x.py"
    assert uri_to_path("file:///c:/proj/x.py") == "C:\\proj\\x.py"


def test_unicode_path():
    p = "C:\\Users\\a0dro\\書類\\メモ.md"
    uri = path_to_uri(p)
    assert uri.startswith("file:///C:/")
    assert "%" in uri  # UTF-8 percent-encoded
    assert uri_to_path(uri) == p


def test_posix_path_roundtrip():
    p = "/home/user/proj/main.py"
    uri = path_to_uri(p)
    assert uri == "file:///home/user/proj/main.py"
    assert uri_to_path(uri) == p


def test_uri_to_path_rejects_non_file():
    import pytest

    with pytest.raises(ValueError):
        uri_to_path("https://example.com/x")
