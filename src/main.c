#include <stdio.h>
#include <string.h>

#include "melisma.h"

enum ml_output_format {
	ML_OUTPUT_IR,
	ML_OUTPUT_ABC,
};

static void
ml_print_usage(const char *program)
{
	fprintf(stderr,
		"usage: %s [--format ir|abc] MIDI_FILE LYRIC_FILE [OUTPUT_FILE]\n",
		program);
}

static const char *
ml_serialization_error(enum ml_output_format format)
{
	if (format == ML_OUTPUT_ABC)
		return "failed to serialize ABC";

	return "failed to serialize bound music IR";
}

int
main(int argc, char **argv)
{
	struct ml_binding_sequence bindings;
	struct ml_note_sequence notes = { 0 };
	struct ml_phrase phrase;
	const char *midi_path;
	const char *lyric_path;
	const char *output_path;
	const char *serialization_error;
	FILE *midi = NULL;
	FILE *lyrics = NULL;
	FILE *output = NULL;
	enum ml_output_format format = ML_OUTPUT_IR;
	int bindings_initialized = 0;
	int output_failed = 0;
	int positional_count;
	int status = 1;
	int i;

	i = 1;
	if (i < argc && strcmp(argv[i], "--format") == 0) {
		if (i + 1 >= argc) {
			ml_print_usage(argv[0]);
			return 2;
		}

		if (strcmp(argv[i + 1], "ir") == 0) {
			format = ML_OUTPUT_IR;
		} else if (strcmp(argv[i + 1], "abc") == 0) {
			format = ML_OUTPUT_ABC;
		} else {
			ml_print_usage(argv[0]);
			return 2;
		}
		i += 2;
	} else if (i < argc && argv[i][0] == '-') {
		ml_print_usage(argv[0]);
		return 2;
	}

	positional_count = argc - i;
	if (positional_count != 2 && positional_count != 3) {
		ml_print_usage(argv[0]);
		return 2;
	}

	if (argv[i][0] == '-' ||
	    argv[i + 1][0] == '-' ||
	    (positional_count == 3 && argv[i + 2][0] == '-')) {
		ml_print_usage(argv[0]);
		return 2;
	}

	midi_path = argv[i];
	lyric_path = argv[i + 1];
	output_path = positional_count == 3 ? argv[i + 2] : NULL;
	serialization_error = ml_serialization_error(format);

	ml_phrase_init(&phrase);

	midi = fopen(midi_path, "rb");
	if (!midi) {
		fprintf(stderr, "failed to open MIDI file: %s\n", midi_path);
		status = 2;
		goto out;
	}

	lyrics = fopen(lyric_path, "r");
	if (!lyrics) {
		fprintf(stderr, "failed to open lyric file: %s\n", lyric_path);
		status = 2;
		goto out;
	}

	if (ml_read_midi_file(midi, &notes) < 0) {
		fprintf(stderr, "failed to import MIDI: %s\n", midi_path);
		goto out;
	}

	if (ml_parse_file(lyrics, &phrase) != 0) {
		fprintf(stderr, "failed to parse lyrics: %s\n", lyric_path);
		goto out;
	}

	if (ml_bind(&phrase, &notes, &bindings) < 0) {
		fprintf(stderr, "failed to bind lyrics to MIDI notes\n");
		goto out;
	}
	bindings_initialized = 1;

	if (output_path) {
		output = fopen(output_path, "w");
		if (!output) {
			fprintf(stderr, "failed to open output file: %s\n",
				output_path);
			status = 2;
			goto out;
		}
	} else {
		output = stdout;
	}

	if ((format == ML_OUTPUT_ABC ?
	     ml_write_abc(output, &notes) :
	     ml_write_bound_ir(output, &notes, &bindings)) < 0 ||
	    fflush(output) == EOF) {
		fprintf(stderr, "%s\n", serialization_error);
		output_failed = 1;
		goto out;
	}

	status = 0;

out:
	if (output && output != stdout) {
		if (fclose(output) == EOF && status == 0) {
			fprintf(stderr, "%s\n", serialization_error);
			output_failed = 1;
			status = 1;
		}
		output = NULL;
	}

	if (output_failed && output_path)
		remove(output_path);

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
