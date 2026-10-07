#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "melisma.h"

struct midi_fixture {
	uint16_t format;
	uint16_t track_count;
	uint16_t division;
	const unsigned char *track;
	size_t track_length;
	const char *header_id;
	const char *track_id;
	uint32_t declared_track_length;
	unsigned int header_extra;
};

static int
write_u16(FILE *output, uint16_t value)
{
	unsigned char data[] = {
		(unsigned char)(value >> 8),
		(unsigned char)value,
	};

	return fwrite(data, 1, sizeof(data), output) == sizeof(data) ? 0 : -1;
}

static int
write_u32(FILE *output, uint32_t value)
{
	unsigned char data[] = {
		(unsigned char)(value >> 24),
		(unsigned char)(value >> 16),
		(unsigned char)(value >> 8),
		(unsigned char)value,
	};

	return fwrite(data, 1, sizeof(data), output) == sizeof(data) ? 0 : -1;
}

static FILE *
create_midi(const struct midi_fixture *fixture)
{
	FILE *input;
	uint32_t track_length;
	unsigned int i;

	input = tmpfile();
	if (!input) {
		perror("tmpfile");
		return NULL;
	}

	track_length = fixture->declared_track_length ?
		       fixture->declared_track_length :
		       (uint32_t)fixture->track_length;

	if (fwrite(fixture->header_id ? fixture->header_id : "MThd",
		   1, 4, input) != 4 ||
	    write_u32(input, 6 + fixture->header_extra) < 0 ||
	    write_u16(input, fixture->format) < 0 ||
	    write_u16(input, fixture->track_count) < 0 ||
	    write_u16(input, fixture->division) < 0)
		goto error;

	for (i = 0; i < fixture->header_extra; ++i) {
		if (fputc(0, input) == EOF)
			goto error;
	}

	if (fwrite(fixture->track_id ? fixture->track_id : "MTrk",
		   1, 4, input) != 4 ||
	    write_u32(input, track_length) < 0 ||
	    fwrite(fixture->track, 1, fixture->track_length, input) !=
		    fixture->track_length)
		goto error;

	rewind(input);
	return input;

error:
	perror("writing MIDI fixture");
	fclose(input);
	return NULL;
}

static int
notes_match(const struct ml_note_sequence *notes,
	    uint32_t ticks_per_quarter,
	    const struct ml_note *expected,
	    size_t count)
{
	size_t i;

	if (notes->ticks_per_quarter != ticks_per_quarter ||
	    notes->count != count)
		return 0;

	for (i = 0; i < count; ++i) {
		if (notes->notes[i].pitch != expected[i].pitch ||
		    notes->notes[i].onset != expected[i].onset ||
		    notes->notes[i].duration != expected[i].duration)
			return 0;
	}

	return count != 0 || notes->notes == NULL;
}

static int
input_is_borrowed(FILE *input, const char *name)
{
	if (fseek(input, 0, SEEK_SET) != 0) {
		fprintf(stderr, "%s input was closed by importer\n", name);
		return 0;
	}

	return 1;
}

static int
expect_success(const char *name,
	       const struct midi_fixture *fixture,
	       const struct ml_note *expected,
	       size_t count)
{
	struct ml_note_sequence notes;
	FILE *input;
	int ret = 1;

	input = create_midi(fixture);
	if (!input)
		return 1;

	if (ml_read_midi_file(input, &notes) < 0) {
		fprintf(stderr, "%s unexpectedly failed\n", name);
		goto out;
	}

	if (!notes_match(&notes, fixture->division, expected, count)) {
		fprintf(stderr, "%s produced unexpected notes\n", name);
		goto out_notes;
	}

	ret = 0;

out_notes:
	if (!input_is_borrowed(input, name))
		ret = 1;

	ml_note_sequence_destroy(&notes);
	if (notes.notes != NULL ||
	    notes.count != 0 ||
	    notes.ticks_per_quarter != 0) {
		fprintf(stderr, "%s destroy did not empty sequence\n", name);
		ret = 1;
	}
out:
	fclose(input);
	return ret;
}

static int
expect_failure(const char *name, const struct midi_fixture *fixture)
{
	struct ml_note_sequence notes = {
		.count = 99,
		.ticks_per_quarter = 99,
	};
	FILE *input;

	input = create_midi(fixture);
	if (!input)
		return 1;

	if (ml_read_midi_file(input, &notes) == 0) {
		fprintf(stderr, "%s unexpectedly succeeded\n", name);
		ml_note_sequence_destroy(&notes);
		fclose(input);
		return 1;
	}

	if (!input_is_borrowed(input, name)) {
		fclose(input);
		return 1;
	}

	fclose(input);
	if (notes.notes != NULL ||
	    notes.count != 0 ||
	    notes.ticks_per_quarter != 0) {
		fprintf(stderr, "%s failure did not empty destination\n", name);
		return 1;
	}

	return 0;
}

static int
expect_raw_failure(const char *name,
		   const unsigned char *data,
		   size_t length)
{
	struct ml_note_sequence notes = {
		.count = 99,
		.ticks_per_quarter = 99,
	};
	FILE *input;
	int ret;

	input = tmpfile();
	if (!input) {
		perror("tmpfile");
		return 1;
	}

	if (fwrite(data, 1, length, input) != length) {
		perror("fwrite");
		fclose(input);
		return 1;
	}
	rewind(input);

	ret = ml_read_midi_file(input, &notes);
	if (!input_is_borrowed(input, name)) {
		fclose(input);
		return 1;
	}
	fclose(input);

	if (ret == 0) {
		fprintf(stderr, "%s unexpectedly succeeded\n", name);
		ml_note_sequence_destroy(&notes);
		return 1;
	}

	if (notes.notes != NULL ||
	    notes.count != 0 ||
	    notes.ticks_per_quarter != 0) {
		fprintf(stderr, "%s failure did not empty destination\n", name);
		return 1;
	}

	return 0;
}

static int
test_valid_notes(void)
{
	static const unsigned char quarter_track[] = {
		0x00, 0x90, 0x3c, 0x40,
		0x83, 0x60, 0x80, 0x3c, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char five_track[] = {
		0x00, 0x90, 0x3c, 0x40, 0x83, 0x60, 0x80, 0x3c, 0x40,
		0x00, 0x90, 0x3e, 0x40, 0x83, 0x60, 0x80, 0x3e, 0x40,
		0x00, 0x90, 0x40, 0x40, 0x83, 0x60, 0x80, 0x40, 0x40,
		0x00, 0x90, 0x41, 0x40, 0x83, 0x60, 0x80, 0x41, 0x40,
		0x00, 0x90, 0x43, 0x40, 0x83, 0x60, 0x80, 0x43, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char rest_track[] = {
		0x00, 0x90, 0x3c, 0x40,
		0x83, 0x60, 0x80, 0x3c, 0x40,
		0x81, 0x70, 0x90, 0x3e, 0x40,
		0x83, 0x60, 0x80, 0x3e, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char zero_velocity_track[] = {
		0x00, 0x90, 0x3c, 0x40,
		0x83, 0x60, 0x90, 0x3c, 0x00,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char running_track[] = {
		0x00, 0x90, 0x3c, 0x40,
		0x83, 0x60, 0x3c, 0x00,
		0x00, 0x3e, 0x40,
		0x83, 0x60, 0x3e, 0x00,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char empty_track[] = {
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char skipped_track[] = {
		0x00, 0xff, 0x01, 0x03, 'a', 'b', 'c',
		0x00, 0xf0, 0x02, 0x01, 0xf7,
		0x00, 0xf7, 0x01, 0x7f,
		0x00, 0xc0, 0x05,
		0x00, 0xd0, 0x20,
		0x00, 0xa0, 0x3c, 0x10,
		0x00, 0xb0, 0x07, 0x64,
		0x00, 0xe0, 0x00, 0x40,
		0x00, 0x90, 0x3c, 0x40,
		0x83, 0x60, 0x80, 0x3c, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char four_byte_vlq_track[] = {
		0x00, 0x90, 0x3c, 0x40,
		0xff, 0xff, 0xff, 0x7f, 0x80, 0x3c, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char nine_track[] = {
		0x00, 0x90, 0x3c, 0x40, 0x83, 0x60, 0x80, 0x3c, 0x40,
		0x00, 0x90, 0x3d, 0x40, 0x83, 0x60, 0x80, 0x3d, 0x40,
		0x00, 0x90, 0x3e, 0x40, 0x83, 0x60, 0x80, 0x3e, 0x40,
		0x00, 0x90, 0x3f, 0x40, 0x83, 0x60, 0x80, 0x3f, 0x40,
		0x00, 0x90, 0x40, 0x40, 0x83, 0x60, 0x80, 0x40, 0x40,
		0x00, 0x90, 0x41, 0x40, 0x83, 0x60, 0x80, 0x41, 0x40,
		0x00, 0x90, 0x42, 0x40, 0x83, 0x60, 0x80, 0x42, 0x40,
		0x00, 0x90, 0x43, 0x40, 0x83, 0x60, 0x80, 0x43, 0x40,
		0x00, 0x90, 0x44, 0x40, 0x83, 0x60, 0x80, 0x44, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const struct ml_note quarter_note[] = {
		{ .pitch = 60, .onset = 0, .duration = 480 },
	};
	static const struct ml_note five_notes[] = {
		{ .pitch = 60, .onset = 0,    .duration = 480 },
		{ .pitch = 62, .onset = 480,  .duration = 480 },
		{ .pitch = 64, .onset = 960,  .duration = 480 },
		{ .pitch = 65, .onset = 1440, .duration = 480 },
		{ .pitch = 67, .onset = 1920, .duration = 480 },
	};
	static const struct ml_note rest_notes[] = {
		{ .pitch = 60, .onset = 0,   .duration = 480 },
		{ .pitch = 62, .onset = 720, .duration = 480 },
	};
	static const struct ml_note running_notes[] = {
		{ .pitch = 60, .onset = 0,   .duration = 480 },
		{ .pitch = 62, .onset = 480, .duration = 480 },
	};
	static const struct ml_note four_byte_vlq_note[] = {
		{ .pitch = 60, .onset = 0, .duration = 268435455 },
	};
	static const struct ml_note nine_notes[] = {
		{ .pitch = 60, .onset = 0,    .duration = 480 },
		{ .pitch = 61, .onset = 480,  .duration = 480 },
		{ .pitch = 62, .onset = 960,  .duration = 480 },
		{ .pitch = 63, .onset = 1440, .duration = 480 },
		{ .pitch = 64, .onset = 1920, .duration = 480 },
		{ .pitch = 65, .onset = 2400, .duration = 480 },
		{ .pitch = 66, .onset = 2880, .duration = 480 },
		{ .pitch = 67, .onset = 3360, .duration = 480 },
		{ .pitch = 68, .onset = 3840, .duration = 480 },
	};
	const struct midi_fixture quarter = {
		.division = 480,
		.track_count = 1,
		.track = quarter_track,
		.track_length = sizeof(quarter_track),
		.header_extra = 2,
	};
	const struct midi_fixture five = {
		.division = 480,
		.track_count = 1,
		.track = five_track,
		.track_length = sizeof(five_track),
	};
	const struct midi_fixture rest = {
		.division = 480,
		.track_count = 1,
		.track = rest_track,
		.track_length = sizeof(rest_track),
	};
	const struct midi_fixture zero_velocity = {
		.division = 480,
		.track_count = 1,
		.track = zero_velocity_track,
		.track_length = sizeof(zero_velocity_track),
	};
	const struct midi_fixture running = {
		.division = 480,
		.track_count = 1,
		.track = running_track,
		.track_length = sizeof(running_track),
	};
	const struct midi_fixture empty = {
		.division = 480,
		.track_count = 1,
		.track = empty_track,
		.track_length = sizeof(empty_track),
	};
	const struct midi_fixture skipped = {
		.division = 480,
		.track_count = 1,
		.track = skipped_track,
		.track_length = sizeof(skipped_track),
	};
	const struct midi_fixture four_byte_vlq = {
		.division = 480,
		.track_count = 1,
		.track = four_byte_vlq_track,
		.track_length = sizeof(four_byte_vlq_track),
	};
	const struct midi_fixture nine = {
		.division = 480,
		.track_count = 1,
		.track = nine_track,
		.track_length = sizeof(nine_track),
	};

	if (expect_success("quarter note", &quarter,
			   quarter_note, 1) ||
	    expect_success("five notes", &five, five_notes, 5) ||
	    expect_success("rest", &rest, rest_notes, 2) ||
	    expect_success("zero-velocity note off", &zero_velocity,
			   quarter_note, 1) ||
	    expect_success("running status", &running, running_notes, 2) ||
	    expect_success("empty track", &empty, NULL, 0) ||
	    expect_success("skipped events", &skipped, quarter_note, 1) ||
	    expect_success("four-byte VLQ", &four_byte_vlq,
			   four_byte_vlq_note, 1) ||
	    expect_success("note-array reallocation", &nine, nine_notes, 9))
		return 1;

	return 0;
}

static int
test_invalid_headers(void)
{
	static const unsigned char empty_track[] = {
		0x00, 0xff, 0x2f, 0x00,
	};
	struct midi_fixture fixture = {
		.track_count = 1,
		.division = 480,
		.track = empty_track,
		.track_length = sizeof(empty_track),
	};

	fixture.format = 1;
	if (expect_failure("format 1", &fixture))
		return 1;

	fixture.format = 2;
	if (expect_failure("format 2", &fixture))
		return 1;

	fixture.format = 0;
	fixture.track_count = 2;
	if (expect_failure("multiple tracks", &fixture))
		return 1;

	fixture.track_count = 1;
	fixture.division = 0xe728;
	if (expect_failure("SMPTE division", &fixture))
		return 1;

	fixture.division = 0;
	if (expect_failure("zero PPQ", &fixture))
		return 1;

	fixture.division = 480;
	fixture.header_id = "Bad!";
	if (expect_failure("invalid MThd", &fixture))
		return 1;

	fixture.header_id = NULL;
	fixture.track_id = "Bad!";
	if (expect_failure("invalid MTrk", &fixture))
		return 1;

	return 0;
}

static int
test_invalid_tracks(void)
{
	static const unsigned char overlong_vlq[] = {
		0x81, 0x80, 0x80, 0x80, 0x00,
	};
	static const unsigned char no_running_status[] = {
		0x00, 0x3c, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char unmatched_off[] = {
		0x00, 0x80, 0x3c, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char unterminated_on[] = {
		0x00, 0x90, 0x3c, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char zero_duration[] = {
		0x00, 0x90, 0x3c, 0x40,
		0x00, 0x80, 0x3c, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char overlap[] = {
		0x00, 0x90, 0x3c, 0x40,
		0x00, 0x90, 0x3e, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char wrong_pitch_off[] = {
		0x00, 0x90, 0x3c, 0x40,
		0x83, 0x60, 0x80, 0x3e, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char wrong_channel_off[] = {
		0x00, 0x90, 0x3c, 0x40,
		0x83, 0x60, 0x81, 0x3c, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char delayed_overlap[] = {
		0x00, 0x90, 0x3c, 0x40,
		0x81, 0x70, 0x90, 0x3e, 0x40,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char running_after_meta[] = {
		0x00, 0xb0, 0x07, 0x64,
		0x00, 0xff, 0x01, 0x00,
		0x00, 0x07, 0x64,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char running_after_f0[] = {
		0x00, 0xb0, 0x07, 0x64,
		0x00, 0xf0, 0x01, 0xf7,
		0x00, 0x07, 0x64,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char running_after_f7[] = {
		0x00, 0xb0, 0x07, 0x64,
		0x00, 0xf7, 0x01, 0x7f,
		0x00, 0x07, 0x64,
		0x00, 0xff, 0x2f, 0x00,
	};
	static const unsigned char truncated_delta_vlq[] = {
		0x81,
	};
	static const unsigned char truncated_meta_vlq[] = {
		0x00, 0xff, 0x01, 0x81,
	};
	static const unsigned char truncated_sysex_vlq[] = {
		0x00, 0xf0, 0x81,
	};
	static const unsigned char nonzero_eot[] = {
		0x00, 0xff, 0x2f, 0x01, 0x00,
	};
	static const unsigned char unsupported_system[] = {
		0x00, 0xf1, 0x00,
	};
	static const unsigned char high_channel_data[] = {
		0x00, 0x90, 0x3c, 0x80,
	};
	static const unsigned char missing_end[] = {
		0x00, 0x90, 0x3c, 0x40,
		0x83, 0x60, 0x80, 0x3c, 0x40,
	};
	static const unsigned char trailing_after_end[] = {
		0x00, 0xff, 0x2f, 0x00, 0x00,
	};
	static const unsigned char truncated_meta[] = {
		0x00, 0xff, 0x01, 0x02, 0x41,
	};
	struct midi_fixture fixture = {
		.track_count = 1,
		.division = 480,
	};

#define EXPECT_BAD_TRACK(name, data)					\
	do {								\
		fixture.track = (data);					\
		fixture.track_length = sizeof(data);			\
		fixture.declared_track_length = 0;			\
		if (expect_failure((name), &fixture))			\
			return 1;					\
	} while (0)

	EXPECT_BAD_TRACK("overlong VLQ", overlong_vlq);
	EXPECT_BAD_TRACK("running status without status", no_running_status);
	EXPECT_BAD_TRACK("unmatched note off", unmatched_off);
	EXPECT_BAD_TRACK("unterminated note on", unterminated_on);
	EXPECT_BAD_TRACK("zero-duration note", zero_duration);
	EXPECT_BAD_TRACK("overlapping notes", overlap);
	EXPECT_BAD_TRACK("wrong-pitch note off", wrong_pitch_off);
	EXPECT_BAD_TRACK("wrong-channel note off", wrong_channel_off);
	EXPECT_BAD_TRACK("delayed overlapping note", delayed_overlap);
	EXPECT_BAD_TRACK("running status after Meta", running_after_meta);
	EXPECT_BAD_TRACK("running status after F0", running_after_f0);
	EXPECT_BAD_TRACK("running status after F7", running_after_f7);
	EXPECT_BAD_TRACK("truncated delta VLQ", truncated_delta_vlq);
	EXPECT_BAD_TRACK("truncated Meta length VLQ", truncated_meta_vlq);
	EXPECT_BAD_TRACK("truncated SysEx length VLQ", truncated_sysex_vlq);
	EXPECT_BAD_TRACK("nonzero EOT payload", nonzero_eot);
	EXPECT_BAD_TRACK("unsupported system status", unsupported_system);
	EXPECT_BAD_TRACK("high channel data byte", high_channel_data);
	EXPECT_BAD_TRACK("missing end of track", missing_end);
	EXPECT_BAD_TRACK("bytes after end of track", trailing_after_end);
	EXPECT_BAD_TRACK("truncated meta event", truncated_meta);

#undef EXPECT_BAD_TRACK

	fixture.track = missing_end;
	fixture.track_length = sizeof(missing_end);
	fixture.declared_track_length = sizeof(missing_end) + 1;
	if (expect_failure("truncated track", &fixture))
		return 1;

	return 0;
}

static int
test_invalid_structure(void)
{
	static const unsigned char short_header_payload[] = {
		'M', 'T', 'h', 'd', 0x00, 0x00, 0x00, 0x05,
	};
	static const unsigned char truncated_header_extra[] = {
		'M', 'T', 'h', 'd', 0x00, 0x00, 0x00, 0x08,
		0x00, 0x00, 0x00, 0x01, 0x01, 0xe0, 0x00,
	};
	static const unsigned char trailing_track[] = {
		0x00, 0xff, 0x2f, 0x00, 0x00,
	};
	const struct midi_fixture trailing = {
		.track_count = 1,
		.division = 480,
		.track = trailing_track,
		.track_length = sizeof(trailing_track),
		.declared_track_length = sizeof(trailing_track) - 1,
	};

	if (expect_raw_failure("short MThd payload",
			       short_header_payload,
			       sizeof(short_header_payload)) ||
	    expect_raw_failure("truncated MThd extra payload",
			       truncated_header_extra,
			       sizeof(truncated_header_extra)) ||
	    expect_failure("bytes after MTrk chunk", &trailing))
		return 1;

	return 0;
}

static int
test_truncated_header(void)
{
	static const unsigned char header[] = {
		'M', 'T', 'h', 'd', 0x00, 0x00, 0x00, 0x06, 0x00,
	};
	struct ml_note_sequence notes = {
		.count = 1,
		.ticks_per_quarter = 1,
	};
	FILE *input;

	input = tmpfile();
	if (!input) {
		perror("tmpfile");
		return 1;
	}

	if (fwrite(header, 1, sizeof(header), input) != sizeof(header)) {
		perror("fwrite");
		fclose(input);
		return 1;
	}
	rewind(input);

	if (ml_read_midi_file(input, &notes) == 0) {
		fprintf(stderr, "truncated header unexpectedly succeeded\n");
		ml_note_sequence_destroy(&notes);
		fclose(input);
		return 1;
	}
	fclose(input);

	if (notes.notes != NULL ||
	    notes.count != 0 ||
	    notes.ticks_per_quarter != 0) {
		fprintf(stderr, "truncated header did not empty destination\n");
		return 1;
	}

	return 0;
}

static int
test_null_arguments(void)
{
	struct ml_note_sequence notes = {
		.count = 1,
		.ticks_per_quarter = 1,
	};
	FILE *input;

	if (ml_read_midi_file(NULL, &notes) == 0 ||
	    notes.notes != NULL ||
	    notes.count != 0 ||
	    notes.ticks_per_quarter != 0) {
		fprintf(stderr, "NULL input was not rejected cleanly\n");
		return 1;
	}

	input = tmpfile();
	if (!input) {
		perror("tmpfile");
		return 1;
	}

	if (ml_read_midi_file(input, NULL) == 0) {
		fprintf(stderr, "NULL destination unexpectedly accepted\n");
		fclose(input);
		return 1;
	}

	fclose(input);
	return 0;
}

int
main(void)
{
	if (test_valid_notes() ||
	    test_invalid_headers() ||
	    test_invalid_tracks() ||
	    test_invalid_structure() ||
	    test_truncated_header() ||
	    test_null_arguments())
		return 1;

	printf("MIDI importer tests passed\n");
	return 0;
}
