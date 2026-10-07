#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "melisma.h"

struct ml_midi_track {
	const unsigned char *data;
	size_t length;
	size_t offset;
};

struct ml_active_note {
	uint64_t onset;
	unsigned int pitch;
	unsigned int channel;
	int active;
};

static int
ml_read_exact(FILE *input, void *data, size_t length)
{
	return fread(data, 1, length, input) == length ? 0 : -1;
}

static int
ml_read_u16(FILE *input, uint16_t *value)
{
	unsigned char data[2];

	if (ml_read_exact(input, data, sizeof(data)) < 0)
		return -1;

	*value = ((uint16_t)data[0] << 8) | data[1];
	return 0;
}

static int
ml_read_u32(FILE *input, uint32_t *value)
{
	unsigned char data[4];

	if (ml_read_exact(input, data, sizeof(data)) < 0)
		return -1;

	*value = ((uint32_t)data[0] << 24) |
		 ((uint32_t)data[1] << 16) |
		 ((uint32_t)data[2] << 8) |
		 data[3];
	return 0;
}

static int
ml_skip_file_bytes(FILE *input, uint32_t length)
{
	unsigned char data[256];
	size_t amount;

	while (length) {
		amount = length < sizeof(data) ? length : sizeof(data);
		if (ml_read_exact(input, data, amount) < 0)
			return -1;
		length -= (uint32_t)amount;
	}

	return 0;
}

static int
ml_track_read_byte(struct ml_midi_track *track, unsigned char *value)
{
	if (track->offset == track->length)
		return -1;

	*value = track->data[track->offset++];
	return 0;
}

static int
ml_track_skip(struct ml_midi_track *track, uint32_t length)
{
	if ((uint64_t)length > track->length - track->offset)
		return -1;

	track->offset += length;
	return 0;
}

static int
ml_track_read_vlq(struct ml_midi_track *track, uint32_t *value)
{
	unsigned char byte;
	uint32_t result = 0;
	unsigned int i;

	for (i = 0; i < 4; ++i) {
		if (ml_track_read_byte(track, &byte) < 0)
			return -1;

		result = (result << 7) | (byte & 0x7f);
		if (!(byte & 0x80)) {
			*value = result;
			return 0;
		}
	}

	return -1;
}

static int
ml_note_append(struct ml_note_sequence *notes,
	       size_t *capacity,
	       unsigned int pitch,
	       uint64_t onset,
	       uint64_t duration)
{
	struct ml_note *new_notes;
	size_t new_capacity;

	if (notes->count == *capacity) {
		if (*capacity) {
			if (*capacity > SIZE_MAX / 2)
				return -1;
			new_capacity = *capacity * 2;
		} else {
			new_capacity = 8;
		}

		if (new_capacity > SIZE_MAX / sizeof(*new_notes))
			return -1;

		new_notes = realloc(notes->notes,
				    new_capacity * sizeof(*new_notes));
		if (!new_notes)
			return -1;

		notes->notes = new_notes;
		*capacity = new_capacity;
	}

	notes->notes[notes->count].pitch = pitch;
	notes->notes[notes->count].onset = onset;
	notes->notes[notes->count].duration = duration;
	++notes->count;
	return 0;
}

static int
ml_channel_data_length(unsigned char status)
{
	switch (status & 0xf0) {
	case 0x80:
	case 0x90:
	case 0xa0:
	case 0xb0:
	case 0xe0:
		return 2;

	case 0xc0:
	case 0xd0:
		return 1;
	}

	return -1;
}

static int
ml_read_channel_event(struct ml_midi_track *track,
		      unsigned char status,
		      int has_first_data,
		      unsigned char first_data,
		      uint64_t tick,
		      struct ml_active_note *active,
		      struct ml_note_sequence *notes,
		      size_t *capacity)
{
	unsigned char data[2];
	int length;
	int i;

	length = ml_channel_data_length(status);
	if (length < 0)
		return -1;

	i = 0;
	if (has_first_data)
		data[i++] = first_data;

	while (i < length) {
		if (ml_track_read_byte(track, &data[i]) < 0)
			return -1;
		++i;
	}

	for (i = 0; i < length; ++i) {
		if (data[i] & 0x80)
			return -1;
	}

	if ((status & 0xf0) == 0x90 && data[1] != 0) {
		if (active->active)
			return -1;

		active->onset = tick;
		active->pitch = data[0];
		active->channel = status & 0x0f;
		active->active = 1;
		return 0;
	}

	if ((status & 0xf0) == 0x80 ||
	    ((status & 0xf0) == 0x90 && data[1] == 0)) {
		if (!active->active ||
		    active->pitch != data[0] ||
		    active->channel != (unsigned int)(status & 0x0f) ||
		    tick == active->onset)
			return -1;

		if (ml_note_append(notes, capacity, active->pitch,
				   active->onset, tick - active->onset) < 0)
			return -1;

		active->active = 0;
	}

	return 0;
}

static int
ml_parse_track(const unsigned char *data,
	       size_t length,
	       struct ml_note_sequence *notes)
{
	struct ml_midi_track track = {
		.data = data,
		.length = length,
	};
	struct ml_active_note active = { 0 };
	unsigned char running_status = 0;
	unsigned char event;
	unsigned char status;
	unsigned char meta_type;
	unsigned char first_data;
	uint32_t delta;
	uint32_t payload_length;
	uint64_t tick = 0;
	size_t capacity = 0;
	int has_first_data;
	int saw_end = 0;

	while (track.offset < track.length) {
		if (ml_track_read_vlq(&track, &delta) < 0 ||
		    UINT64_MAX - tick < delta)
			return -1;
		tick += delta;

		if (ml_track_read_byte(&track, &event) < 0)
			return -1;

		has_first_data = !(event & 0x80);
		if (has_first_data) {
			if (!running_status)
				return -1;
			status = running_status;
			first_data = event;
		} else {
			status = event;
			first_data = 0;
		}

		if (status >= 0x80 && status <= 0xef) {
			if (!has_first_data)
				running_status = status;
			if (ml_read_channel_event(&track, status,
						  has_first_data, first_data,
						  tick, &active, notes,
						  &capacity) < 0)
				return -1;
			continue;
		}

		running_status = 0;

		if (status == 0xff) {
			if (ml_track_read_byte(&track, &meta_type) < 0 ||
			    ml_track_read_vlq(&track, &payload_length) < 0)
				return -1;

			if (meta_type == 0x2f) {
				if (payload_length != 0 ||
				    track.offset != track.length ||
				    active.active)
					return -1;
				saw_end = 1;
				break;
			}

			if (ml_track_skip(&track, payload_length) < 0)
				return -1;
			continue;
		}

		if (status == 0xf0 || status == 0xf7) {
			if (ml_track_read_vlq(&track, &payload_length) < 0 ||
			    ml_track_skip(&track, payload_length) < 0)
				return -1;
			continue;
		}

		return -1;
	}

	if (!saw_end || active.active)
		return -1;

	return 0;
}

int
ml_read_midi_file(FILE *input, struct ml_note_sequence *notes)
{
	struct ml_note_sequence result = { 0 };
	unsigned char chunk_id[4];
	unsigned char *track_data = NULL;
	uint32_t header_length;
	uint32_t track_length;
	uint16_t format;
	uint16_t track_count;
	uint16_t division;
	int trailing;
	int ret = -1;

	if (!notes)
		return -1;

	notes->notes = NULL;
	notes->count = 0;
	notes->ticks_per_quarter = 0;

	if (!input)
		return -1;

	if (ml_read_exact(input, chunk_id, sizeof(chunk_id)) < 0 ||
	    memcmp(chunk_id, "MThd", sizeof(chunk_id)) != 0 ||
	    ml_read_u32(input, &header_length) < 0 ||
	    header_length < 6 ||
	    ml_read_u16(input, &format) < 0 ||
	    ml_read_u16(input, &track_count) < 0 ||
	    ml_read_u16(input, &division) < 0)
		goto out;

	if (ml_skip_file_bytes(input, header_length - 6) < 0 ||
	    format != 0 ||
	    track_count != 1 ||
	    (division & 0x8000) ||
	    division == 0)
		goto out;

	if (ml_read_exact(input, chunk_id, sizeof(chunk_id)) < 0 ||
	    memcmp(chunk_id, "MTrk", sizeof(chunk_id)) != 0 ||
	    ml_read_u32(input, &track_length) < 0)
		goto out;

	if ((uint64_t)track_length > SIZE_MAX)
		goto out;

	if (track_length) {
		track_data = malloc(track_length);
		if (!track_data ||
		    ml_read_exact(input, track_data, track_length) < 0)
			goto out;
	}

	trailing = fgetc(input);
	if (trailing != EOF || ferror(input))
		goto out;

	result.ticks_per_quarter = division;
	if (ml_parse_track(track_data, track_length, &result) < 0)
		goto out;

	*notes = result;
	result.notes = NULL;
	ret = 0;

out:
	free(track_data);
	ml_note_sequence_destroy(&result);
	if (ret < 0) {
		notes->notes = NULL;
		notes->count = 0;
		notes->ticks_per_quarter = 0;
	}
	return ret;
}

void
ml_note_sequence_destroy(struct ml_note_sequence *notes)
{
	free(notes->notes);

	notes->notes = NULL;
	notes->count = 0;
	notes->ticks_per_quarter = 0;
}
