#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include "melisma.h"

static const unsigned int expected_pitches[] = {
	60, 62, 64, 65, 60, 62, 64, 65, 60, 62, 64, 65,
};

static const char expected_ir[] =
	"{\"type\":\"header\",\"version\":1,\"ticks_per_quarter\":480}\n"
	"{\"type\":\"note\",\"pitch\":60,\"onset\":0,\"duration\":480,\"binding\":\"untexted\",\"syllable\":null}\n"
	"{\"type\":\"note\",\"pitch\":62,\"onset\":480,\"duration\":480,\"binding\":\"untexted\",\"syllable\":null}\n"
	"{\"type\":\"note\",\"pitch\":64,\"onset\":960,\"duration\":480,\"binding\":\"untexted\",\"syllable\":null}\n"
	"{\"type\":\"note\",\"pitch\":65,\"onset\":1440,\"duration\":480,\"binding\":\"untexted\",\"syllable\":null}\n"
	"{\"type\":\"note\",\"pitch\":60,\"onset\":1920,\"duration\":480,\"binding\":\"onset\",\"syllable\":\"Glo\"}\n"
	"{\"type\":\"note\",\"pitch\":62,\"onset\":2400,\"duration\":480,\"binding\":\"onset\",\"syllable\":\"ri\"}\n"
	"{\"type\":\"note\",\"pitch\":64,\"onset\":2880,\"duration\":480,\"binding\":\"onset\",\"syllable\":\"a\"}\n"
	"{\"type\":\"note\",\"pitch\":65,\"onset\":3360,\"duration\":480,\"binding\":\"continuation\",\"syllable\":\"a\"}\n"
	"{\"type\":\"note\",\"pitch\":60,\"onset\":3840,\"duration\":480,\"binding\":\"untexted\",\"syllable\":null}\n"
	"{\"type\":\"note\",\"pitch\":62,\"onset\":4320,\"duration\":480,\"binding\":\"untexted\",\"syllable\":null}\n"
	"{\"type\":\"note\",\"pitch\":64,\"onset\":4800,\"duration\":480,\"binding\":\"untexted\",\"syllable\":null}\n"
	"{\"type\":\"note\",\"pitch\":65,\"onset\":5280,\"duration\":480,\"binding\":\"untexted\",\"syllable\":null}\n";

static const char expected_abc[] =
	"X:1\n"
	"T:\n"
	"M:4/4\n"
	"L:1/32\n"
	"Q:1/4=120\n"
	"V: Vocal clef=treble name=\"Vocal Melody\" snm=\"Vocal\"\n"
	"V: Ins clef=treble name=\"Ins Melody\" snm=\"Inst.\"\n"
	"K:C\n"
	"V: Vocal\n"
	"=C8 =D8 =E8 =F8 | =C8 =D8 =E8 =F8 | =C8 =D8 =E8 =F8 |\n"
	"V: Ins\n"
	"Z3 |\n";

static int
path_join(char *path, size_t size, const char *directory, const char *name)
{
	int length = snprintf(path, size, "%s/%s", directory, name);
	return length < 0 || (size_t)length >= size ? -1 : 0;
}

static char *
read_stream(FILE *stream)
{
	char *data;
	long size;

	if (fflush(stream) == EOF ||
	    fseek(stream, 0, SEEK_END) != 0 ||
	    (size = ftell(stream)) < 0 ||
	    fseek(stream, 0, SEEK_SET) != 0)
		return NULL;

	data = malloc((size_t)size + 1);
	if (!data)
		return NULL;
	if (fread(data, 1, (size_t)size, stream) != (size_t)size) {
		free(data);
		return NULL;
	}
	data[size] = '\0';
	return data;
}

static char *
read_path(const char *path)
{
	FILE *input = fopen(path, "rb");
	char *data;

	if (!input)
		return NULL;
	data = read_stream(input);
	fclose(input);
	return data;
}

static size_t
count_text(const char *text, const char *needle)
{
	size_t count = 0;
	size_t length = strlen(needle);

	while ((text = strstr(text, needle)) != NULL) {
		++count;
		text += length;
	}
	return count;
}

static int
ustx_matches_timeline(const char *ustx)
{
	static const char *const notes[] = {
		"  - position: 1920\n"
		"    duration: 480\n"
		"    tone: 60\n"
		"    lyric: \"Glo\"\n",
		"  - position: 2400\n"
		"    duration: 480\n"
		"    tone: 62\n"
		"    lyric: \"ri\"\n",
		"  - position: 2880\n"
		"    duration: 480\n"
		"    tone: 64\n"
		"    lyric: \"a\"\n",
		"  - position: 3360\n"
		"    duration: 480\n"
		"    tone: 65\n"
		"    lyric: \"+\"\n",
	};
	size_t i;

	if (!strstr(ustx, "- duration: 5760\n") ||
	    !strstr(ustx, "  position: 0\n") ||
	    count_text(ustx, "  - position: ") != 4 ||
	    count_text(ustx, "    phoneme_expressions: []\n") != 4 ||
	    count_text(ustx, "    phoneme_overrides: []\n") != 4 ||
	    strstr(ustx, "    lyric: \"\"\n"))
		return 0;

	for (i = 0; i < sizeof(notes) / sizeof(notes[0]); ++i) {
		if (!strstr(ustx, notes[i]))
			return 0;
	}
	return 1;
}

static int
load_fixture(const char *source_root,
	     struct ml_note_sequence *notes,
	     struct ml_phrase *phrase)
{
	char midi_path[1024];
	char lyric_path[1024];
	FILE *midi;
	FILE *lyrics;
	int result = -1;

	if (path_join(midi_path, sizeof(midi_path), source_root,
		      "examples/intro-vocal-interlude.mid") < 0 ||
	    path_join(lyric_path, sizeof(lyric_path), source_root,
		      "examples/intro-vocal-interlude.txt") < 0)
		return -1;

	midi = fopen(midi_path, "rb");
	lyrics = fopen(lyric_path, "r");
	if (!midi || !lyrics)
		goto out;

	ml_phrase_init(phrase);
	if (ml_read_midi_file(midi, notes) < 0 ||
	    ml_parse_file(lyrics, phrase) != 0)
		goto out_phrase;

	result = 0;
	goto out;

out_phrase:
	ml_note_sequence_destroy(notes);
	ml_phrase_destroy(phrase);
out:
	if (midi)
		fclose(midi);
	if (lyrics)
		fclose(lyrics);
	return result;
}

static int
test_direct_timeline(const char *source_root)
{
	struct ml_note_sequence notes = { 0 };
	struct ml_phrase phrase;
	struct ml_binding_sequence bindings;
	struct ml_ustx_config config = {
		"TIGER_DS_v102",
		"OpenUtau.Core.DiffSinger.DiffSingerARPAPlusEnglishPhonemizer",
		"DIFFSINGER",
	};
	FILE *output = NULL;
	char *actual = NULL;
	size_t i;
	int ret = 1;

	if (load_fixture(source_root, &notes, &phrase) < 0 ||
	    notes.count != 12 || notes.ticks_per_quarter != 480)
		return 1;

	for (i = 0; i < notes.count; ++i) {
		if (notes.notes[i].pitch != expected_pitches[i] ||
		    notes.notes[i].onset != i * 480 ||
		    notes.notes[i].duration != 480)
			goto out;
	}

	if (ml_bind(&phrase, &notes, &bindings) < 0 || bindings.count != 12)
		goto out;

	for (i = 0; i < bindings.count; ++i) {
		enum ml_binding_type expected_type;
		const char *expected_syllable = NULL;

		if (i < 4 || i >= 8) {
			expected_type = ML_BINDING_UNTEXTED;
		} else if (i == 7) {
			expected_type = ML_BINDING_CONTINUATION;
			expected_syllable = "a";
		} else {
			expected_type = ML_BINDING_ONSET;
			expected_syllable = i == 4 ? "Glo" : i == 5 ? "ri" : "a";
		}

		if (bindings.bindings[i].note != &notes.notes[i] ||
		    bindings.bindings[i].type != expected_type ||
		    (expected_syllable == NULL) !=
			(bindings.bindings[i].syllable == NULL) ||
		    (expected_syllable &&
		     (bindings.bindings[i].syllable->text.length !=
		      strlen(expected_syllable) ||
		      memcmp(bindings.bindings[i].syllable->text.data,
			     expected_syllable,
			     strlen(expected_syllable)) != 0)))
			goto out_bindings;
	}

	output = tmpfile();
	if (!output ||
	    ml_write_bound_ir(output, &notes, &bindings) < 0 ||
	    !(actual = read_stream(output)) ||
	    strcmp(actual, expected_ir) != 0)
		goto out_bindings;
	free(actual);
	actual = NULL;
	fclose(output);
	output = NULL;

	output = tmpfile();
	if (!output ||
	    ml_write_abc(output, &notes) < 0 ||
	    !(actual = read_stream(output)) ||
	    strcmp(actual, expected_abc) != 0)
		goto out_bindings;
	free(actual);
	actual = NULL;
	fclose(output);
	output = NULL;

	output = tmpfile();
	if (!output ||
	    ml_write_ustx(output, &notes, &bindings, &config) < 0 ||
	    !(actual = read_stream(output)) ||
	    !ustx_matches_timeline(actual))
		goto out_bindings;

	ret = 0;
out_bindings:
	if (output)
		fclose(output);
	free(actual);
	ml_binding_sequence_destroy(&bindings);
out:
	ml_phrase_destroy(&phrase);
	ml_note_sequence_destroy(&notes);
	return ret;
}

static int
bundle_has_exact_files(const char *path)
{
	static const char *const expected[] = {
		"accompaniment.abc", "bound.jsonl", "vocal.ustx",
	};
	DIR *directory = opendir(path);
	struct dirent *entry;
	size_t count = 0;
	size_t i;
	int found;

	if (!directory)
		return 0;
	while ((entry = readdir(directory)) != NULL) {
		if (strcmp(entry->d_name, ".") == 0 ||
		    strcmp(entry->d_name, "..") == 0)
			continue;
		found = 0;
		for (i = 0; i < sizeof(expected) / sizeof(expected[0]); ++i) {
			if (strcmp(entry->d_name, expected[i]) == 0)
				found = 1;
		}
		if (!found) {
			closedir(directory);
			return 0;
		}
		++count;
	}
	closedir(directory);
	return count == sizeof(expected) / sizeof(expected[0]);
}

static int
run_render_inputs(const char *melisma,
		  const char *midi,
		  const char *lyrics,
		  const char *bundle)
{
	pid_t pid;
	int status;

	pid = fork();
	if (pid < 0)
		return -1;
	if (pid == 0) {
		execl(melisma, melisma, "render-inputs",
		      "--singer", "TIGER_DS_v102",
		      "--phonemizer",
		      "OpenUtau.Core.DiffSinger.DiffSingerARPAPlusEnglishPhonemizer",
		      "--renderer", "DIFFSINGER",
		      midi, lyrics, bundle, (char *)NULL);
		_exit(127);
	}
	if (waitpid(pid, &status, 0) < 0 || !WIFEXITED(status))
		return -1;
	return WEXITSTATUS(status);
}

static int
test_render_inputs(const char *melisma,
		   const char *build_root,
		   const char *source_root)
{
	char root[1024];
	char bundle[1024];
	char midi[1024];
	char lyrics[1024];
	char ir_path[1024];
	char ustx_path[1024];
	char abc_path[1024];
	char *ir = NULL;
	char *ustx = NULL;
	char *abc = NULL;
	int ret = 1;

	if (snprintf(root, sizeof(root), "%s/timeline-regression-%ld",
		     build_root, (long)getpid()) >= (int)sizeof(root) ||
	    path_join(bundle, sizeof(bundle), root, "bundle") < 0 ||
	    path_join(midi, sizeof(midi), source_root,
		      "examples/intro-vocal-interlude.mid") < 0 ||
	    path_join(lyrics, sizeof(lyrics), source_root,
		      "examples/intro-vocal-interlude.txt") < 0 ||
	    path_join(ir_path, sizeof(ir_path), bundle, "bound.jsonl") < 0 ||
	    path_join(ustx_path, sizeof(ustx_path), bundle, "vocal.ustx") < 0 ||
	    path_join(abc_path, sizeof(abc_path), bundle, "accompaniment.abc") < 0 ||
	    mkdir(root, 0700) < 0)
		return 1;

	if (run_render_inputs(melisma, midi, lyrics, bundle) != 0 ||
	    !bundle_has_exact_files(bundle) ||
	    !(ir = read_path(ir_path)) ||
	    !(ustx = read_path(ustx_path)) ||
	    !(abc = read_path(abc_path)) ||
	    strcmp(ir, expected_ir) != 0 ||
	    strcmp(abc, expected_abc) != 0 ||
	    !ustx_matches_timeline(ustx) ||
	    !strstr(ir, "\"onset\":1920") ||
	    !strstr(ustx, "  - position: 1920\n") ||
	    !strstr(abc, "=C8 =D8 =E8 =F8 | =C8"))
		goto out;

	ret = 0;
out:
	free(ir);
	free(ustx);
	free(abc);
	unlink(ir_path);
	unlink(ustx_path);
	unlink(abc_path);
	rmdir(bundle);
	rmdir(root);
	return ret;
}

static int
test_instrumental_interlude(void)
{
	struct ml_syllable syllables[] = {
		{ .text = { .data = "one", .length = 3 } },
		{ .text = { .data = "two", .length = 3 } },
	};
	struct ml_note notes_data[] = {
		{ .pitch = 60, .onset = 0, .duration = 480 },
		{ .pitch = 62, .onset = 480, .duration = 480 },
		{ .pitch = 64, .onset = 960, .duration = 480 },
	};
	struct ml_note_sequence notes = {
		.notes = notes_data, .count = 3, .ticks_per_quarter = 480,
	};
	struct ml_binding binding_data[] = {
		{ ML_BINDING_ONSET, &notes_data[0], &syllables[0] },
		{ ML_BINDING_UNTEXTED, &notes_data[1], NULL },
		{ ML_BINDING_ONSET, &notes_data[2], &syllables[1] },
	};
	struct ml_binding_sequence bindings = {
		.bindings = binding_data, .count = 3, .capacity = 3,
	};
	struct ml_ustx_config config = { "", "", "" };
	FILE *output = tmpfile();
	char *ustx;
	int result;

	if (!output || ml_write_ustx(output, &notes, &bindings, &config) < 0)
		return 1;
	ustx = read_stream(output);
	fclose(output);
	if (!ustx)
		return 1;
	result = count_text(ustx, "  - position: ") != 2 ||
		 !strstr(ustx, "  - position: 0\n") ||
		 !strstr(ustx, "  - position: 960\n") ||
		 strstr(ustx, "  - position: 480\n");
	free(ustx);
	return result;
}

int
main(int argc, char **argv)
{
	if (argc != 4)
		return 1;
	if (test_direct_timeline(argv[3])) {
		fprintf(stderr, "direct timeline regression failed\n");
		return 1;
	}
	if (test_render_inputs(argv[1], argv[2], argv[3])) {
		fprintf(stderr, "render-inputs timeline regression failed\n");
		return 1;
	}
	if (test_instrumental_interlude()) {
		fprintf(stderr, "instrumental interlude regression failed\n");
		return 1;
	}
	printf("timeline regression passed\n");
	return 0;
}
