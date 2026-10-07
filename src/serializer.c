#include <inttypes.h>
#include <stdio.h>

#include "melisma.h"

static int
ml_write_json_string(FILE *output, const char *data, size_t length)
{
	size_t i;
	unsigned char ch;

	if (fputc('"', output) == EOF)
		return -1;

	for (i = 0; i < length; ++i) {
		ch = (unsigned char)data[i];

		switch (ch) {
		case '"':
			if (fputs("\\\"", output) == EOF)
				return -1;
			break;

		case '\\':
			if (fputs("\\\\", output) == EOF)
				return -1;
			break;

		case '\b':
			if (fputs("\\b", output) == EOF)
				return -1;
			break;

		case '\f':
			if (fputs("\\f", output) == EOF)
				return -1;
			break;

		case '\n':
			if (fputs("\\n", output) == EOF)
				return -1;
			break;

		case '\r':
			if (fputs("\\r", output) == EOF)
				return -1;
			break;

		case '\t':
			if (fputs("\\t", output) == EOF)
				return -1;
			break;

		default:
			if (ch < 0x20) {
				if (fprintf(output, "\\u%04x", ch) < 0)
					return -1;
			} else if (fputc(ch, output) == EOF) {
				return -1;
			}
			break;
		}
	}

	if (fputc('"', output) == EOF)
		return -1;

	return 0;
}

static const char *
ml_binding_type_name(enum ml_binding_type type)
{
	switch (type) {
	case ML_BINDING_ONSET:
		return "onset";

	case ML_BINDING_CONTINUATION:
		return "continuation";

	case ML_BINDING_UNTEXTED:
		return "untexted";
	}

	return NULL;
}

static int
ml_validate_bound_ir(const struct ml_note_sequence *notes,
		     const struct ml_binding_sequence *bindings)
{
	const struct ml_binding *binding;
	size_t i;

	if (!notes->ticks_per_quarter)
		return -1;

	if (notes->count != bindings->count)
		return -1;

	if (notes->count && (!notes->notes || !bindings->bindings))
		return -1;

	for (i = 0; i < bindings->count; ++i) {
		binding = &bindings->bindings[i];

		if (binding->note != &notes->notes[i])
			return -1;

		switch (binding->type) {
		case ML_BINDING_ONSET:
		case ML_BINDING_CONTINUATION:
			if (!binding->syllable)
				return -1;

			if (binding->syllable->text.length &&
			    !binding->syllable->text.data)
				return -1;
			break;

		case ML_BINDING_UNTEXTED:
			if (binding->syllable)
				return -1;
			break;

		default:
			return -1;
		}
	}

	return 0;
}

int
ml_write_bound_ir(FILE *output,
		  const struct ml_note_sequence *notes,
		  const struct ml_binding_sequence *bindings)
{
	const struct ml_binding *binding;
	const struct ml_note *note;
	const char *binding_name;
	size_t i;

	if (!output || !notes || !bindings)
		return -1;

	if (ml_validate_bound_ir(notes, bindings) < 0)
		return -1;

	if (fprintf(output,
		    "{\"type\":\"header\",\"version\":1,"
		    "\"ticks_per_quarter\":%" PRIu32 "}\n",
		    notes->ticks_per_quarter) < 0)
		return -1;

	for (i = 0; i < bindings->count; ++i) {
		binding = &bindings->bindings[i];
		note = binding->note;
		binding_name = ml_binding_type_name(binding->type);

		if (fprintf(output,
			    "{\"type\":\"note\","
			    "\"pitch\":%u,"
			    "\"onset\":%" PRIu64 ","
			    "\"duration\":%" PRIu64 ","
			    "\"binding\":\"%s\","
			    "\"syllable\":",
			    note->pitch,
			    note->onset,
			    note->duration,
			    binding_name) < 0)
			return -1;

		if (binding->type == ML_BINDING_UNTEXTED) {
			if (fputs("null", output) == EOF)
				return -1;
		} else if (ml_write_json_string(output,
						binding->syllable->text.data,
						binding->syllable->text.length) < 0) {
			return -1;
		}

		if (fputs("}\n", output) == EOF)
			return -1;
	}

	if (ferror(output))
		return -1;

	return 0;
}
