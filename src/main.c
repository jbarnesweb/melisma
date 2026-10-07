#include <stdio.h>

#include "melisma.h"

int
main(int argc, char **argv)
{
	struct ml_binding_sequence bindings;
	struct ml_note_sequence notes = { 0 };
	struct ml_phrase phrase;
	FILE *midi = NULL;
	FILE *lyrics = NULL;
	FILE *output = NULL;
	int bindings_initialized = 0;
	int output_failed = 0;
	int status = 1;

	if (argc != 3 && argc != 4) {
		fprintf(stderr,
			"usage: %s MIDI_FILE LYRIC_FILE [OUTPUT_FILE]\n",
			argv[0]);
		return 2;
	}

	ml_phrase_init(&phrase);

	midi = fopen(argv[1], "rb");
	if (!midi) {
		fprintf(stderr, "failed to open MIDI file: %s\n", argv[1]);
		status = 2;
		goto out;
	}

	lyrics = fopen(argv[2], "r");
	if (!lyrics) {
		fprintf(stderr, "failed to open lyric file: %s\n", argv[2]);
		status = 2;
		goto out;
	}

	if (ml_read_midi_file(midi, &notes) < 0) {
		fprintf(stderr, "failed to import MIDI: %s\n", argv[1]);
		goto out;
	}

	if (ml_parse_file(lyrics, &phrase) != 0) {
		fprintf(stderr, "failed to parse lyrics: %s\n", argv[2]);
		goto out;
	}

	if (ml_bind(&phrase, &notes, &bindings) < 0) {
		fprintf(stderr, "failed to bind lyrics to MIDI notes\n");
		goto out;
	}
	bindings_initialized = 1;

	if (argc == 4) {
		output = fopen(argv[3], "w");
		if (!output) {
			fprintf(stderr, "failed to open output file: %s\n", argv[3]);
			status = 2;
			goto out;
		}
	} else {
		output = stdout;
	}

	if (ml_write_bound_ir(output, &notes, &bindings) < 0 ||
	    fflush(output) == EOF) {
		fprintf(stderr, "failed to serialize bound music IR\n");
		output_failed = 1;
		goto out;
	}

	status = 0;

out:
	if (output && output != stdout) {
		if (fclose(output) == EOF && status == 0) {
			fprintf(stderr, "failed to serialize bound music IR\n");
			output_failed = 1;
			status = 1;
		}
		output = NULL;
	}

	if (output_failed && argc == 4)
		remove(argv[3]);

	if (bindings_initialized)
		ml_binding_sequence_destroy(&bindings);
	ml_phrase_destroy(&phrase);
	ml_note_sequence_destroy(&notes);

	if (lyrics)
		fclose(lyrics);
	if (midi)
		fclose(midi);

	return status;
}
