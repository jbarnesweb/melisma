#include <dirent.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "melisma.h"

enum ml_output_format {
	ML_OUTPUT_IR,
	ML_OUTPUT_ABC,
	ML_OUTPUT_USTX,
};

static void
ml_print_usage(const char *program)
{
	fprintf(stderr,
		"usage: %s [--format ir|abc|ustx] [--singer NAME] "
		"[--phonemizer CLASS] [--renderer NAME] "
		"MIDI_FILE LYRIC_FILE [OUTPUT_FILE]\n"
		"       %s render-inputs [--singer NAME] "
		"[--phonemizer CLASS] [--renderer NAME] "
		"MIDI_FILE LYRIC_FILE OUTPUT_DIR\n",
		program, program);
}

static const char *
ml_serialization_error(enum ml_output_format format)
{
	if (format == ML_OUTPUT_ABC)
		return "failed to serialize ABC";
	if (format == ML_OUTPUT_USTX)
		return "failed to serialize USTX";

	return "failed to serialize bound music IR";
}

static int
ml_serialize(FILE *output,
	     enum ml_output_format format,
	     const struct ml_note_sequence *notes,
	     const struct ml_binding_sequence *bindings,
	     const struct ml_ustx_config *ustx_config)
{
	switch (format) {
	case ML_OUTPUT_IR:
		return ml_write_bound_ir(output, notes, bindings);
	case ML_OUTPUT_ABC:
		return ml_write_abc(output, notes);
	case ML_OUTPUT_USTX:
		return ml_write_ustx(output, notes, bindings, ustx_config);
	}

	return -1;
}

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

static int
ml_bundle_path(char *path,
	       size_t size,
	       const char *directory,
	       const char *name)
{
	int length;

	length = snprintf(path, size, "%s/%s", directory, name);
	return length < 0 || (size_t)length >= size ? -1 : 0;
}

static int
ml_prepare_bundle_directory(const char *path)
{
	struct dirent *entry;
	DIR *directory;

	if (mkdir(path, 0777) < 0 && errno != EEXIST) {
		fprintf(stderr, "failed to create output directory %s: %s\n",
			path, strerror(errno));
		return -1;
	}

	directory = opendir(path);
	if (!directory) {
		fprintf(stderr, "failed to open output directory %s: %s\n",
			path, strerror(errno));
		return -1;
	}

	errno = 0;
	while ((entry = readdir(directory)) != NULL) {
		if (strcmp(entry->d_name, ".") != 0 &&
		    strcmp(entry->d_name, "..") != 0) {
			fprintf(stderr,
				"output directory is not empty: %s\n", path);
			closedir(directory);
			return -1;
		}
	}

	if (errno) {
		fprintf(stderr, "failed to inspect output directory %s: %s\n",
			path, strerror(errno));
		closedir(directory);
		return -1;
	}

	if (closedir(directory) < 0) {
		fprintf(stderr, "failed to close output directory %s: %s\n",
			path, strerror(errno));
		return -1;
	}

	return 0;
}

static FILE *
ml_open_bundle_file(const char *path, const char *artifact)
{
	FILE *output;

	output = fopen(path, "w");
	if (!output)
		fprintf(stderr, "failed to open temporary %s output: %s\n",
			artifact, strerror(errno));
	return output;
}

static int
ml_finish_bundle_file(FILE **output, const char *artifact)
{
	if (fflush(*output) == EOF) {
		fprintf(stderr, "failed to flush %s output: %s\n",
			artifact, strerror(errno));
		fclose(*output);
		*output = NULL;
		return -1;
	}

	if (fclose(*output) == EOF) {
		fprintf(stderr, "failed to close %s output: %s\n",
			artifact, strerror(errno));
		*output = NULL;
		return -1;
	}

	*output = NULL;
	return 0;
}

static int
ml_render_inputs(const char *directory,
		 const struct ml_note_sequence *notes,
		 const struct ml_binding_sequence *bindings,
		 const struct ml_ustx_config *ustx_config)
{
	static const char *const names[] = {
		"bound.jsonl",
		"vocal.ustx",
		"accompaniment.abc",
	};
	char final_paths[3][PATH_MAX] = { { 0 } };
	char temporary_names[3][64];
	char temporary_paths[3][PATH_MAX] = { { 0 } };
	FILE *output = NULL;
	size_t published = 0;
	size_t i;
	int status = -1;

	if (ml_prepare_bundle_directory(directory) < 0)
		return -1;

	for (i = 0; i < 3; ++i) {
		if (snprintf(temporary_names[i], sizeof(temporary_names[i]),
			     ".%s.tmp.%ld", names[i], (long)getpid()) >=
		    (int)sizeof(temporary_names[i]) ||
		    ml_bundle_path(final_paths[i], sizeof(final_paths[i]),
				   directory, names[i]) < 0 ||
		    ml_bundle_path(temporary_paths[i],
				   sizeof(temporary_paths[i]),
				   directory, temporary_names[i]) < 0) {
			fprintf(stderr, "output directory path is too long: %s\n",
				directory);
			goto out;
		}
	}

	output = ml_open_bundle_file(temporary_paths[0], names[0]);
	if (!output)
		goto out;
	if (ml_write_bound_ir(output, notes, bindings) < 0) {
		fprintf(stderr, "failed to serialize %s\n", names[0]);
		goto out;
	}
	if (ml_finish_bundle_file(&output, names[0]) < 0)
		goto out;

	output = ml_open_bundle_file(temporary_paths[1], names[1]);
	if (!output)
		goto out;
	if (ml_write_ustx(output, notes, bindings, ustx_config) < 0) {
		fprintf(stderr, "failed to serialize %s\n", names[1]);
		goto out;
	}
	if (ml_finish_bundle_file(&output, names[1]) < 0)
		goto out;

	output = ml_open_bundle_file(temporary_paths[2], names[2]);
	if (!output)
		goto out;
	if (ml_write_abc(output, notes) < 0) {
		fprintf(stderr, "failed to serialize %s\n", names[2]);
		goto out;
	}
	if (ml_finish_bundle_file(&output, names[2]) < 0)
		goto out;

	for (i = 0; i < 3; ++i) {
		if (rename(temporary_paths[i], final_paths[i]) < 0) {
			fprintf(stderr, "failed to publish %s: %s\n",
				names[i], strerror(errno));
			goto out;
		}
		++published;
	}

	status = 0;

out:
	if (output)
		fclose(output);

	if (status != 0) {
		for (i = 0; i < 3; ++i) {
			if (temporary_paths[i][0])
				remove(temporary_paths[i]);
		}
		for (i = 0; i < published; ++i) {
			if (final_paths[i][0])
				remove(final_paths[i]);
		}
	}

	return status;
}

int
main(int argc, char **argv)
{
	struct ml_binding_sequence bindings;
	struct ml_note_sequence notes = { 0 };
	struct ml_phrase phrase;
	struct ml_ustx_config ustx_config = { "", "", "" };
	const char *midi_path;
	const char *lyric_path;
	const char *output_path;
	const char *serialization_error;
	FILE *midi = NULL;
	FILE *lyrics = NULL;
	FILE *output = NULL;
	enum ml_output_format format = ML_OUTPUT_IR;
	int bindings_initialized = 0;
	int format_seen = 0;
	int output_failed = 0;
	int phonemizer_seen = 0;
	int positional_count;
	int renderer_seen = 0;
	int render_inputs = 0;
	int singer_seen = 0;
	int status = 1;
	int i;

	i = 1;
	if (i < argc && strcmp(argv[i], "render-inputs") == 0) {
		render_inputs = 1;
		++i;
	}

	while (i < argc && argv[i][0] == '-') {
		const char *value;

		if (i + 1 >= argc || !argv[i + 1][0] ||
		    argv[i + 1][0] == '-') {
			ml_print_usage(argv[0]);
			return 2;
		}
		value = argv[i + 1];

		if (strcmp(argv[i], "--format") == 0) {
			if (render_inputs || format_seen) {
				ml_print_usage(argv[0]);
				return 2;
			}
			format_seen = 1;

			if (strcmp(value, "ir") == 0) {
				format = ML_OUTPUT_IR;
			} else if (strcmp(value, "abc") == 0) {
				format = ML_OUTPUT_ABC;
			} else if (strcmp(value, "ustx") == 0) {
				format = ML_OUTPUT_USTX;
			} else {
				ml_print_usage(argv[0]);
				return 2;
			}
		} else if (strcmp(argv[i], "--singer") == 0 && !singer_seen) {
			ustx_config.singer = value;
			singer_seen = 1;
		} else if (strcmp(argv[i], "--phonemizer") == 0 &&
			   !phonemizer_seen) {
			ustx_config.phonemizer = value;
			phonemizer_seen = 1;
		} else if (strcmp(argv[i], "--renderer") == 0 &&
			   !renderer_seen) {
			ustx_config.renderer = value;
			renderer_seen = 1;
		} else {
			ml_print_usage(argv[0]);
			return 2;
		}
		i += 2;
	}

	if ((singer_seen || phonemizer_seen || renderer_seen) &&
	    !render_inputs && format != ML_OUTPUT_USTX) {
		ml_print_usage(argv[0]);
		return 2;
	}

	positional_count = argc - i;
	if ((render_inputs && positional_count != 3) ||
	    (!render_inputs && positional_count != 2 &&
	     positional_count != 3)) {
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

	if (render_inputs) {
		if (ml_render_inputs(output_path, &notes, &bindings,
				     &ustx_config) < 0)
			goto out;
		status = 0;
		goto out;
	}

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

	if (ml_serialize(output, format, &notes, &bindings, &ustx_config) < 0 ||
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
