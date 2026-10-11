#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static const char expected_ir[] =
	"{\"type\":\"header\",\"version\":1,\"ticks_per_quarter\":480}\n"
	"{\"type\":\"note\",\"pitch\":60,\"onset\":0,\"duration\":480,\"binding\":\"onset\",\"syllable\":\"Glo\"}\n"
	"{\"type\":\"note\",\"pitch\":62,\"onset\":480,\"duration\":480,\"binding\":\"onset\",\"syllable\":\"ri\"}\n"
	"{\"type\":\"note\",\"pitch\":64,\"onset\":960,\"duration\":480,\"binding\":\"onset\",\"syllable\":\"a\"}\n"
	"{\"type\":\"note\",\"pitch\":65,\"onset\":1440,\"duration\":480,\"binding\":\"continuation\",\"syllable\":\"a\"}\n"
	"{\"type\":\"note\",\"pitch\":67,\"onset\":1920,\"duration\":480,\"binding\":\"continuation\",\"syllable\":\"a\"}\n";

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
	"=C8 =D8 =E8 =F8 | =G8 z24 |\n"
	"V: Ins\n"
	"Z2 |\n";

static int
path_join(char *path, size_t size, const char *directory, const char *name)
{
	int length;

	length = snprintf(path, size, "%s/%s", directory, name);
	return length < 0 || (size_t)length >= size ? -1 : 0;
}

static int
file_matches(const char *path, const char *expected)
{
	char data[8192];
	FILE *input;
	size_t length;
	int extra;

	input = fopen(path, "rb");
	if (!input)
		return 0;

	length = strlen(expected);
	if (length >= sizeof(data) ||
	    fread(data, 1, length, input) != length) {
		fclose(input);
		return 0;
	}

	extra = fgetc(input);
	fclose(input);
	return extra == EOF && memcmp(data, expected, length) == 0;
}

static int
file_contains(const char *path, const char *text)
{
	char data[8192];
	FILE *input;
	size_t length;

	input = fopen(path, "rb");
	if (!input)
		return 0;

	length = fread(data, 1, sizeof(data) - 1, input);
	if (ferror(input)) {
		fclose(input);
		return 0;
	}
	fclose(input);

	data[length] = '\0';
	return strstr(data, text) != NULL;
}

static int
run_cli(const char *program,
	char *const arguments[],
	const char *stdout_path,
	const char *stderr_path)
{
	FILE *stream;
	pid_t pid;
	int status;

	pid = fork();
	if (pid < 0)
		return -1;

	if (pid == 0) {
		stream = fopen(stdout_path, "w");
		if (!stream)
			_exit(127);
		if (dup2(fileno(stream), STDOUT_FILENO) < 0)
			_exit(127);
		fclose(stream);

		stream = fopen(stderr_path, "w");
		if (!stream)
			_exit(127);
		if (dup2(fileno(stream), STDERR_FILENO) < 0)
			_exit(127);
		fclose(stream);

		execv(program, arguments);
		_exit(127);
	}

	if (waitpid(pid, &status, 0) < 0 || !WIFEXITED(status))
		return -1;
	return WEXITSTATUS(status);
}

static int
expect_status(const char *name,
	      const char *program,
	      char *const arguments[],
	      const char *stdout_path,
	      const char *stderr_path,
	      int expected)
{
	int actual;

	actual = run_cli(program, arguments, stdout_path, stderr_path);
	if (actual != expected) {
		fprintf(stderr, "%s returned %d, expected %d\n",
			name, actual, expected);
		return 1;
	}
	return 0;
}

static int
bundle_has_exact_files(const char *path)
{
	static const char *const expected[] = {
		"accompaniment.abc",
		"bound.jsonl",
		"vocal.ustx",
	};
	struct dirent *entry;
	DIR *directory;
	size_t count = 0;
	size_t i;
	int found;

	directory = opendir(path);
	if (!directory)
		return 0;

	while ((entry = readdir(directory)) != NULL) {
		if (strcmp(entry->d_name, ".") == 0 ||
		    strcmp(entry->d_name, "..") == 0)
			continue;

		found = 0;
		for (i = 0; i < 3; ++i) {
			if (strcmp(entry->d_name, expected[i]) == 0) {
				found = 1;
				break;
			}
		}
		if (!found) {
			closedir(directory);
			return 0;
		}
		++count;
	}

	closedir(directory);
	return count == 3;
}

static void
remove_bundle(const char *directory)
{
	static const char *const names[] = {
		"bound.jsonl",
		"vocal.ustx",
		"accompaniment.abc",
		"sentinel",
	};
	char path[1024];
	size_t i;

	for (i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
		if (path_join(path, sizeof(path), directory, names[i]) == 0)
			unlink(path);
	}
	rmdir(directory);
}

int
main(int argc, char **argv)
{
	char root[1024] = "";
	char bundle[1024] = "";
	char nonempty[1024] = "";
	char readonly[1024] = "";
	char invalid[1024] = "";
	char midi[1024] = "";
	char lyrics[1024] = "";
	char ir[1024] = "";
	char ustx[1024] = "";
	char abc[1024] = "";
	char sentinel[1024] = "";
	char stdout_path[1024] = "";
	char stderr_path[1024] = "";
	char *arguments[16];
	FILE *output;
	int ret = 1;

	if (argc != 4)
		return 1;

	if (snprintf(root, sizeof(root), "%s/render-inputs-test-%ld",
		     argv[2], (long)getpid()) >= (int)sizeof(root) ||
	    mkdir(root, 0700) < 0)
		return 1;

	if (path_join(bundle, sizeof(bundle), root, "bundle") < 0 ||
	    path_join(nonempty, sizeof(nonempty), root, "nonempty") < 0 ||
	    path_join(readonly, sizeof(readonly), root, "readonly") < 0 ||
	    path_join(invalid, sizeof(invalid), root, "missing/bundle") < 0 ||
	    path_join(midi, sizeof(midi), argv[3],
		      "examples/five-notes.mid") < 0 ||
	    path_join(lyrics, sizeof(lyrics), argv[3],
		      "examples/gloria.txt") < 0 ||
	    path_join(ir, sizeof(ir), bundle, "bound.jsonl") < 0 ||
	    path_join(ustx, sizeof(ustx), bundle, "vocal.ustx") < 0 ||
	    path_join(abc, sizeof(abc), bundle, "accompaniment.abc") < 0 ||
	    path_join(sentinel, sizeof(sentinel), nonempty, "sentinel") < 0 ||
	    path_join(stdout_path, sizeof(stdout_path), root, "stdout.txt") < 0 ||
	    path_join(stderr_path, sizeof(stderr_path), root, "stderr.txt") < 0)
		goto out;

	arguments[0] = argv[1];
	arguments[1] = "render-inputs";
	arguments[2] = "--singer";
	arguments[3] = "TIGER_DS_v102";
	arguments[4] = "--phonemizer";
	arguments[5] =
		"OpenUtau.Core.DiffSinger.DiffSingerARPAPlusEnglishPhonemizer";
	arguments[6] = "--renderer";
	arguments[7] = "DIFFSINGER";
	arguments[8] = midi;
	arguments[9] = lyrics;
	arguments[10] = bundle;
	arguments[11] = NULL;

	if (expect_status("render-inputs", argv[1], arguments,
			  stdout_path, stderr_path, 0) ||
	    !bundle_has_exact_files(bundle) ||
	    !file_matches(ir, expected_ir) ||
	    !file_matches(abc, expected_abc) ||
	    !file_contains(ustx, "    tone: 60\n    lyric: \"Glo\"\n") ||
	    !file_contains(ustx, "    tone: 62\n    lyric: \"ri\"\n") ||
	    !file_contains(ustx, "    tone: 64\n    lyric: \"a\"\n") ||
	    !file_contains(ustx, "    tone: 65\n    lyric: \"+\"\n") ||
	    !file_contains(ustx, "    tone: 67\n    lyric: \"+\"\n") ||
	    !file_contains(ustx, "singer: \"TIGER_DS_v102\"") ||
	    !file_contains(
		    ustx,
		    "phonemizer: \"OpenUtau.Core.DiffSinger.DiffSingerARPAPlusEnglishPhonemizer\"") ||
	    !file_contains(ustx, "renderer: \"DIFFSINGER\"") ||
	    !file_matches(stderr_path, "")) {
		fprintf(stderr, "rendered bundle did not match\n");
		goto out;
	}

	if (mkdir(nonempty, 0700) < 0)
		goto out;
	output = fopen(sentinel, "w");
	if (!output)
		goto out;
	fclose(output);
	arguments[2] = midi;
	arguments[3] = lyrics;
	arguments[4] = nonempty;
	arguments[5] = NULL;
	if (expect_status("non-empty output directory", argv[1], arguments,
			  stdout_path, stderr_path, 1) ||
	    !file_contains(stderr_path, "output directory is not empty") ||
	    access(sentinel, F_OK) != 0)
		goto out;

	arguments[4] = invalid;
	if (expect_status("invalid output path", argv[1], arguments,
			  stdout_path, stderr_path, 1) ||
	    !file_contains(stderr_path, "failed to create output directory"))
		goto out;

	arguments[4] = NULL;
	if (expect_status("missing output directory", argv[1], arguments,
			  stdout_path, stderr_path, 2) ||
	    !file_contains(stderr_path, "usage:"))
		goto out;

	arguments[2] = "--singer";
	arguments[3] = "first";
	arguments[4] = "--singer";
	arguments[5] = "second";
	arguments[6] = midi;
	arguments[7] = lyrics;
	arguments[8] = readonly;
	arguments[9] = NULL;
	if (expect_status("duplicate render option", argv[1], arguments,
			  stdout_path, stderr_path, 2) ||
	    !file_contains(stderr_path, "usage:"))
		goto out;

	arguments[2] = "--phonemizer";
	arguments[3] = NULL;
	if (expect_status("missing render option", argv[1], arguments,
			  stdout_path, stderr_path, 2) ||
	    !file_contains(stderr_path, "usage:"))
		goto out;

	arguments[2] = "--renderer";
	arguments[3] = "";
	arguments[4] = midi;
	arguments[5] = lyrics;
	arguments[6] = readonly;
	arguments[7] = NULL;
	if (expect_status("empty render option", argv[1], arguments,
			  stdout_path, stderr_path, 2) ||
	    !file_contains(stderr_path, "usage:"))
		goto out;

	if (mkdir(readonly, 0500) < 0)
		goto out;
	arguments[2] = midi;
	arguments[3] = lyrics;
	arguments[4] = readonly;
	arguments[5] = NULL;
	if (expect_status("unwritable bundle output", argv[1], arguments,
			  stdout_path, stderr_path, 1) ||
	    !file_contains(stderr_path,
			   "failed to open temporary bound.jsonl output"))
		goto out;

	ret = 0;

out:
	chmod(readonly, 0700);
	remove_bundle(readonly);
	remove_bundle(nonempty);
	remove_bundle(bundle);
	unlink(stderr_path);
	unlink(stdout_path);
	rmdir(root);

	if (ret == 0)
		printf("render-inputs CLI tests passed\n");
	return ret;
}
