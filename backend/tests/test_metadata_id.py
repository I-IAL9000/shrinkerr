def test_parse_media_id_recognizes_poster_id_formats():
    """v0.9.89: language-lookup id parsing is aligned with the poster system's
    _extract_ids, so TMDB ids and brace/suffix forms resolve (previously only
    [tt…]/[tvdb-…] did, leaving TMDB-tagged items stuck as heuristic)."""
    from backend.metadata import parse_media_id
    assert parse_media_id("/m/Movie (2002) [tt0315642]/x.mkv") == ("imdb", "tt0315642")
    assert parse_media_id("/m/Show [tvdb-311947]/S01/x.mkv") == ("tvdb", "311947")
    assert parse_media_id("/m/Movie (2022) {tmdb-12345}/x.mkv") == ("tmdb", "12345")
    assert parse_media_id("/m/Movie [tmdbid-777]/x.mkv") == ("tmdb", "777")
    assert parse_media_id("/m/Show [tvdbid-999]/x.mkv") == ("tvdb", "999")
    # id can live on the filename, not just the folder
    assert parse_media_id("/m/Plain/Movie.2019.{tmdb-42}.mkv") == ("tmdb", "42")
    # no id anywhere → None (these still need manual matching)
    assert parse_media_id("/m/Plain Movie (2019)/x.mkv") is None
