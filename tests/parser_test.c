#include <stdio.h>
#include <string.h>

#include "melisma.h"

static int
parse_string(const char *text, struct ml_phrase *phrase)
{
	FILE *input;
	int ret;

	input = tmpfile();
	if (!input) {
		perror("tmpfile");
		return -1;
	}

	if (fputs(text, input) == EOF) {
		perror("fputs");
		fclose(input);
		return -1;
	}

	rewind(input);

	ml_phrase_init(phrase);
	ret = ml_parse_file(input, phrase);

	fclose(input);
	return ret;
}

static int
syllable_matches(const struct ml_element *element,
		 const char *text,
		 unsigned int continuations)
{
	size_t length = strlen(text);

	if (element->type != ML_ELEMENT_SYLLABLE)
		return 0;

	if (element->syllable.text.length != length)
		return 0;

	if (memcmp(element->syllable.text.data, text, length) != 0)
		return 0;

	return element->syllable.continuations == continuations;
}

static int
test_basic_melisma(void)
{
	struct ml_phrase phrase;
	int ret = 1;

	if (parse_string("Glo - ri - a -- --\n", &phrase) != 0) {
		fprintf(stderr, "basic melisma parse failed\n");
		return 1;
	}

	if (phrase.count != 3) {
		fprintf(stderr, "expected 3 elements, got %zu\n",
			phrase.count);
		goto out;
	}

	if (!syllable_matches(&phrase.elements[0], "Glo", 0) ||
	    !syllable_matches(&phrase.elements[1], "ri", 0) ||
	    !syllable_matches(&phrase.elements[2], "a", 2)) {
		fprintf(stderr, "basic melisma phrase does not match expected result\n");
		goto out;
	}

	ret = 0;

out:
	ml_phrase_destroy(&phrase);
	return ret;
}

static int
test_permissive_separator(void)
{
	struct ml_phrase phrase;
	int ret = 1;

	if (parse_string("Glo - -- -- ri - a\n", &phrase) != 0) {
		fprintf(stderr, "permissive separator parse failed\n");
		return 1;
	}

	if (phrase.count != 3) {
		fprintf(stderr, "expected 3 elements, got %zu\n",
			phrase.count);
		goto out;
	}

	if (!syllable_matches(&phrase.elements[0], "Glo", 2) ||
	    !syllable_matches(&phrase.elements[1], "ri", 0) ||
	    !syllable_matches(&phrase.elements[2], "a", 0)) {
		fprintf(stderr, "permissive separator phrase does not match expected result\n");
		goto out;
	}

	ret = 0;

out:
	ml_phrase_destroy(&phrase);
	return ret;
}

static int
test_untexted(void)
{
	struct ml_phrase phrase;
	int ret = 1;

	if (parse_string("Glo - ri _ a -- --\n", &phrase) != 0) {
		fprintf(stderr, "untexted parse failed\n");
		return 1;
	}

	if (phrase.count != 4) {
		fprintf(stderr, "expected 4 elements, got %zu\n",
			phrase.count);
		goto out;
	}

	if (!syllable_matches(&phrase.elements[0], "Glo", 0) ||
	    !syllable_matches(&phrase.elements[1], "ri", 0) ||
	    phrase.elements[2].type != ML_ELEMENT_UNTEXTED ||
	    !syllable_matches(&phrase.elements[3], "a", 2)) {
		fprintf(stderr, "untexted phrase does not match expected result\n");
		goto out;
	}

	ret = 0;

out:
	ml_phrase_destroy(&phrase);
	return ret;
}

static int
test_invalid_melisma(void)
{
	struct ml_phrase phrase;

	if (parse_string("Glo --- ri\n", &phrase) == 0) {
		fprintf(stderr, "invalid melisma unexpectedly parsed\n");
		ml_phrase_destroy(&phrase);
		return 1;
	}

	ml_phrase_destroy(&phrase);
	return 0;
}

int
main(void)
{
	if (test_basic_melisma())
		return 1;

	if (test_permissive_separator())
		return 1;

	if (test_untexted())
		return 1;

	if (test_invalid_melisma())
		return 1;

	printf("parser tests passed\n");
	return 0;
}

