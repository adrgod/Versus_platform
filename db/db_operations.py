import sqlite3

def db_create_schema(con):

    cur = con.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS tbl_artist(artist_id INTEGER PRIMARY KEY, artist_name TEXT UNIQUE, when_loaded DATETIME)")

    cur.execute("CREATE TABLE IF NOT EXISTS tbl_album(album_id INTEGER PRIMARY KEY, artist_id, album_name, disc_number DEFAULT 1, release_year, genre, album_cover BLOB, band_photo BLOB, when_loaded DATETIME)")

    #An album can have the same name for different artists. An artist can have two albums with same name, but an album shouldn't have the same name, artist, year and disc number. makes sense?
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_album_artist_name_number_year
        ON tbl_album (artist_id, album_name, disc_number, release_year)
    """)

    cur.execute("CREATE TABLE IF NOT EXISTS tbl_track(track_id INTEGER PRIMARY KEY, album_id, track_name, track_number, track_length INTEGER, dq_confidence FLOAT, when_loaded DATETIME)")

    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_track_album_name_number
        ON tbl_track (album_id, track_name, track_number)
    """)

    
def db_load_data(con, records):

    cur = con.cursor()

    artists_data = [
        {"artist": record["artist"]}
        for record in records
    ]


    cur.executemany(
        """INSERT OR IGNORE INTO tbl_artist (artist_name, when_loaded)
        VALUES (:artist, CURRENT_TIMESTAMP)
        """,
        artists_data,
    )
    artist_ids = dict(
        cur.execute(
            "SELECT artist_name, artist_id FROM tbl_artist"
        ).fetchall()
    )

    albums = []
    tracks = []

    for record in records:
        artist_id = artist_ids[record["artist"]]

        albums.append({
            "artist_id": artist_id,
            "album": record["album"],
            "discnumber": record["discnumber"] or 1,
            "date": record["date"],
            "genre": record["genre"],
            "album_cover": record["album_cover"],
        })

    cur.executemany(
        """
        INSERT INTO tbl_album 
        (artist_id, album_name, disc_number, release_year, genre, album_cover, when_loaded)
        VALUES
            (:artist_id, :album, :discnumber, :date, :genre, :album_cover, CURRENT_TIMESTAMP)
        ON CONFLICT (artist_id, album_name, disc_number, release_year)
        DO UPDATE SET album_cover = COALESCE(tbl_album.album_cover, excluded.album_cover)
        """,
        albums
    )

    album_ids = {
        (artist_id, album_name): album_id
        for artist_id, album_name, album_id in cur.execute(
            """
            SELECT artist_id, album_name, album_id
            FROM tbl_album
            """
        )
    }

    for record in records:
        album_id = album_ids[artist_ids[record["artist"]], record["album"]]

        tracks.append({
            "album_id": album_id,
            "title": record["title"],
            "track_number": record["tracknumber"],
            "length": record["length"],
            "dq_confidence": record["dq_confidence"]
        })

    cur.executemany(
        """
        INSERT OR IGNORE INTO tbl_track
            (album_id, track_name, track_number, track_length, dq_confidence, when_loaded)
        VALUES
            (:album_id, :title, :track_number, :length, :dq_confidence ,CURRENT_TIMESTAMP)
        """,
        tracks,
    )

def load_db_data(db_file_location, data):
    con = sqlite3.connect(db_file_location)
    
    #validate_tag_with_path(records, mp3_files)

    db_create_schema(con)
    db_load_data(con, data)

    con.commit()
    con.close()