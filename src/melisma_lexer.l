%option noyywrap
%option nodefault
%option reentrant
%option bison-bridge
%option bison-locations

%{
#include <stdlib.h>
#include <string.h>

#include "melisma_parser.tab.h"

static int
copy_text(struct ml_string *dst, const char *src, size_t length)
{
	dst->data = malloc(length + 1);
	if (!dst->data)
		return -1;

	memcpy(dst->data, src, length);
	dst->data[length] = '\0';
	dst->length = length;

	return 0;
}

static void
advance_location(YYLTYPE *loc, const char *text, size_t length)
{
	size_t i;

	loc->last_line = loc->first_line;
	loc->last_column = loc->first_column;

	for (i = 0; i < length; ++i) {
		if (text[i] == '\n') {
			++loc->last_line;
			loc->last_column = 1;
		} else {
			++loc->last_column;
		}
	}
}

#define YY_USER_ACTION						\
	do {							\
		yylloc->first_line = yylloc->last_line;		\
		yylloc->first_column = yylloc->last_column;	\
		advance_location(yylloc, yytext, yyleng);	\
	} while (0);
%}

WSPACE		[ \t\r\n]+
SYLLABLE	[A-Za-z]+

%%

{WSPACE} {
	/* Ignore whitespace */
}

"-"{3,}	{
	return TOK_INVALID;
}

"--" {
	return TOK_MELISMA;
}

"-" {
	/* allow but ignore */
}

"_" {
	return TOK_UNTEXTED;
}

"|" {
	return TOK_BAR;
}

{SYLLABLE}	{
	if (copy_text(&yylval->text, yytext, yyleng) < 0) {
		return TOK_INVALID;
	}

	return TOK_SYLLABLE;
}

"#"[^\r\n]*	{
	/* Ignore comments, but preserve location advancement. */
}

.	{
	return TOK_INVALID;
}

<<EOF>>	{
	return 0;
}

%%
