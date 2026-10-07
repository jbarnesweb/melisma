#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static const unsigned char midi_data[] = {
	'M', 'T', 'h', 'd',
	0x00, 0x00, 0x00, 0x06,
	0x00, 0x00,
	0x00, 0x01,
	0x01, 0xe0,
	'M', 'T', 'r', 'k',
	0x00, 0x00, 0x00, 0x31,
	0x00, 0x90, 0x3c, 0x40, 0x83, 0x60, 0x80, 0x3c, 0x40,
	0x00, 0x90, 0x3e, 0x40, 0x83, 0x60, 0x80, 0x3e, 0x40,
	0x00, 0x90, 0x40, 0x40, 0x83, 0x60, 0x80, 0x40, 0x40,
	0x00, 0x90, 0x41, 0x40, 0x83, 0x60, 0x80, 0x41, 0x40,
	0x00, 0x90, 0x43, 0x40, 0x83, 0x60, 0x80, 0x43, 0x40,
	0x00, 0xff, 0x2f, 0x00,
};

static const char expected_json[] =
	"{\"type\":\"header\",\"version\":1,\"ticks_per_quarter\":480}\n"
	"{\"type\":\"note\",\"pitch\":60,\"onset\":0,\"duration\":480,\"binding\":\"onset\",\"syllable\":\"Glo\"}\n"
	"{\"type\":\"note\",\"pitch\":62,\"onset\":480,\"duration\":480,\"binding\":\"onset\",\"syllable\":\"ri\"}\n"
	"{\"type\":\"note\",\"pitch\":64,\"onset\":960,\"duration\":480,\"binding\":\"onset\",\"syllable\":\"a\"}\n"
	"{\"type\":\"note\",\"pitch\":65,\"onset\":1440,\"duration\":480,\"binding\":\"continuation\",\"syllable\":\"a\"}\n"
	"{\"type\":\"note\",\"pitch\":67,\"onset\":1920,\"duration\":480,\"binding\":\"continuation\",\"syllable\":\"a\"}\n";

static int
write_file(const char *path, const void *data, size_t length)
{
	FILE *output;
	int ret = -1;

	output = fopen(path, "wb");
	if (!output) {
		perror(path);
		return -1;
	}

	if (fwrite(data, 1, length, output) == length)
		ret = 0;
	else
		perror(path);

	if (fclose(output) != 0) {
		perror(path);
		ret = -1;
	}

	return ret;
}

static int
file_matches(const char *path, const char *expected)
{
	char data[1024];
	FILE *input;
	size_t length;
	int extra;

	input = fopen(path, "rb");
	if (!input) {
		perror(path);
		return 0;
	}

	length = strlen(expected);
	if (length >= sizeof(data)) {
		fclose(input);
		return 0;
	}

	if (fread(data, 1, length, input) != length) {
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
	char data[1024];
	FILE *input;
	size_t length;

	input = fopen(path, "rb");
	if (!input) {
		perror(path);
		return 0;
	}

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
	if (pid < 0) {
		perror("fork");
		return -1;
	}

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

	if (waitpid(pid, &status, 0) < 0) {
		perror("waitpid");
		return -1;
	}

	if (!WIFEXITED(status))
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
path_join(char *path, size_t size, const char *directory, const char *name)
{
	int length;

	length = snprintf(path, size, "%s/%s", directory, name);
	return length < 0 || (size_t)length >= size ? -1 : 0;
}

int
main(int argc, char **argv)
{
	char directory[1024] = "";
	char midi_path[1024] = "";
	char lyric_path[1024] = "";
	char bad_midi_path[1024] = "";
	char bad_lyric_path[1024] = "";
	char mismatch_path[1024] = "";
	char output_path[1024] = "";
	char stdout_path[1024] = "";
	char stderr_path[1024] = "";
	char failed_output_path[1024] = "";
	char full_output_path[1024] = "";
	char missing_path[1024] = "";
	char *wrong_arguments[2];
	char *file_arguments[5];
	char *stdout_arguments[4];
	int ret = 1;

	if (argc != 3) {
		fprintf(stderr, "usage: %s MELISMA BUILD_DIRECTORY\n", argv[0]);
		return 1;
	}

	if (snprintf(directory, sizeof(directory), "%s/cli-test-%ld",
		     argv[2], (long)getpid()) >= (int)sizeof(directory) ||
	    mkdir(directory, 0700) < 0) {
		perror("creating CLI test directory");
		return 1;
	}

#define MAKE_PATH(variable, name)					\
	do {								\
		if (path_join((variable), sizeof(variable),		\
			      directory, (name)) < 0)			\
			goto out;					\
	} while (0)

	MAKE_PATH(midi_path, "notes.mid");
	MAKE_PATH(lyric_path, "lyrics.txt");
	MAKE_PATH(bad_midi_path, "bad.mid");
	MAKE_PATH(bad_lyric_path, "bad-lyrics.txt");
	MAKE_PATH(mismatch_path, "mismatch.txt");
	MAKE_PATH(output_path, "output.jsonl");
	MAKE_PATH(stdout_path, "stdout.txt");
	MAKE_PATH(stderr_path, "stderr.txt");
	MAKE_PATH(failed_output_path, "failed.jsonl");
	MAKE_PATH(full_output_path, "full.jsonl");
	MAKE_PATH(missing_path, "missing");

#undef MAKE_PATH

	if (write_file(midi_path, midi_data, sizeof(midi_data)) < 0 ||
	    write_file(lyric_path, "Glo - ri - a -- --\n",
		       strlen("Glo - ri - a -- --\n")) < 0 ||
	    write_file(bad_midi_path, "bad", strlen("bad")) < 0 ||
	    write_file(bad_lyric_path, "Glo --- ri\n",
		       strlen("Glo --- ri\n")) < 0 ||
	    write_file(mismatch_path, "Glo\n", strlen("Glo\n")) < 0)
		goto out;

	wrong_arguments[0] = argv[1];
	wrong_arguments[1] = NULL;
	if (expect_status("wrong argument count", argv[1], wrong_arguments,
			  stdout_path, stderr_path, 2))
		goto out;

	file_arguments[0] = argv[1];
	file_arguments[1] = missing_path;
	file_arguments[2] = lyric_path;
	file_arguments[3] = NULL;
	if (expect_status("missing MIDI", argv[1], file_arguments,
			  stdout_path, stderr_path, 2))
		goto out;

	file_arguments[1] = midi_path;
	file_arguments[2] = missing_path;
	if (expect_status("missing lyrics", argv[1], file_arguments,
			  stdout_path, stderr_path, 2))
		goto out;

	file_arguments[1] = bad_midi_path;
	file_arguments[2] = lyric_path;
	file_arguments[3] = failed_output_path;
	file_arguments[4] = NULL;
	if (expect_status("malformed MIDI", argv[1], file_arguments,
			  stdout_path, stderr_path, 1) ||
	    !file_contains(stderr_path, "failed to import MIDI") ||
	    access(failed_output_path, F_OK) == 0) {
		fprintf(stderr, "failed pipeline created output file\n");
		goto out;
	}

	file_arguments[1] = midi_path;
	file_arguments[2] = bad_lyric_path;
	if (expect_status("malformed lyrics", argv[1], file_arguments,
			  stdout_path, stderr_path, 1) ||
	    !file_contains(stderr_path, "failed to parse lyrics") ||
	    file_contains(stderr_path,
			  "failed to bind lyrics to MIDI notes") ||
	    access(failed_output_path, F_OK) == 0)
		goto out;

	file_arguments[2] = mismatch_path;
	if (expect_status("binding mismatch", argv[1], file_arguments,
			  stdout_path, stderr_path, 1) ||
	    !file_contains(stderr_path,
			   "failed to bind lyrics to MIDI notes") ||
	    access(failed_output_path, F_OK) == 0)
		goto out;

	file_arguments[2] = lyric_path;
	file_arguments[3] = directory;
	if (expect_status("unwritable output", argv[1], file_arguments,
			  stdout_path, stderr_path, 2) ||
	    !file_contains(stderr_path, "failed to open output file"))
		goto out;

	file_arguments[3] = output_path;
	if (expect_status("explicit output", argv[1], file_arguments,
			  stdout_path, stderr_path, 0) ||
	    !file_matches(output_path, expected_json)) {
		fprintf(stderr, "explicit output did not match expected JSONL\n");
		goto out;
	}

	stdout_arguments[0] = argv[1];
	stdout_arguments[1] = midi_path;
	stdout_arguments[2] = lyric_path;
	stdout_arguments[3] = NULL;
	if (expect_status("stdout output", argv[1], stdout_arguments,
			  stdout_path, stderr_path, 0) ||
	    !file_matches(stdout_path, expected_json)) {
		fprintf(stderr, "stdout did not match expected JSONL\n");
		goto out;
	}

	if (symlink("/dev/full", full_output_path) < 0) {
		perror("symlink");
		goto out;
	}

	file_arguments[3] = full_output_path;
	if (expect_status("serialization failure", argv[1], file_arguments,
			  stdout_path, stderr_path, 1) ||
	    access(full_output_path, F_OK) == 0) {
		fprintf(stderr, "failed serialization left output file\n");
		goto out;
	}

	ret = 0;

out:
	unlink(full_output_path);
	unlink(failed_output_path);
	unlink(output_path);
	unlink(stderr_path);
	unlink(stdout_path);
	unlink(mismatch_path);
	unlink(bad_lyric_path);
	unlink(bad_midi_path);
	unlink(lyric_path);
	unlink(midi_path);
	if (rmdir(directory) < 0 && errno != ENOENT)
		perror("removing CLI test directory");

	if (ret == 0)
		printf("CLI integration tests passed\n");
	return ret;
}
