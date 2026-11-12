from backend.db import require_row, unpack


def meeting_row(db, meeting_id):
    return require_row(db, "SELECT * FROM meetings WHERE id=?", (meeting_id,), "Встреча не найдена")


def transcript(db, meeting_id):
    rows = db.execute(
        """SELECT t.id,t.start_ms,t.end_ms,t.speaker_id,t.overlap,t.text,s.name AS speaker_name,
           s.source AS speaker_source,s.number FROM transcript_segments t
           LEFT JOIN meeting_speakers s ON s.id=t.speaker_id WHERE t.meeting_id=? ORDER BY t.start_ms,t.id""",
        (meeting_id,),
    )
    return [{
        **{key: row[key] for key in ("id", "start_ms", "end_ms", "speaker_id", "speaker_name", "speaker_source", "text")},
        "overlap": bool(row["overlap"]),
        "speaker_label": (f"Спикер {row['number']}" if row["number"] else
                          "Перекрытие голосов" if row["overlap"] else "Спикер не определён"),
    } for row in rows]


def safe_job(row):
    return {**{key: row[key] for key in ("id", "kind", "state", "created_at", "updated_at")},
            "error": unpack(row["error"]), "result": unpack(row["result"])}
