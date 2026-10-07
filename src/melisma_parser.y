%{
#include <stdio.h>
#include <stdlib.h>

%}

%code requires {
#include "melisma.h"

typedef void *yyscan_t;

}

%define api.pure full
%define parse.error detailed
%locations

%union {
	struct ml_string text;
	struct ml_syllable syllable;
	unsigned int count;
}

%code provides {
	int yylex(YYSTYPE *yylval, YYLTYPE *yylloc, yyscan_t scanner);
	void yyerror(YYLTYPE *loc, yyscan_t scanner, struct ml_phrase *phrase, const char *message);
}

%token <text> TOK_SYLLABLE

%token TOK_MELISMA
%token TOK_UNTEXTED
%token TOK_BAR
%token TOK_INVALID

%type <syllable> syllable
%type <count> melisma_sequence

%start phrase

%parse-param { yyscan_t scanner }
%parse-param { struct ml_phrase *phrase }
%lex-param { yyscan_t scanner }
%destructor { free($$.data); } <text>
%destructor { free($$.text.data); } <syllable>
%%

phrase:
	syllable_sequence 
	| syllable_sequence TOK_BAR
	;

syllable_sequence:
	lyrical_element
	| syllable_sequence lyrical_element
	;

lyrical_element:
	syllable
	{
		struct ml_element element;

		element.type = ML_ELEMENT_SYLLABLE;
		element.syllable = $1;
		if (ml_phrase_append(phrase, element) < 0)
			YYNOMEM;
	}
	| TOK_UNTEXTED
	{
		struct ml_element element = { 0 };
		element.type = ML_ELEMENT_UNTEXTED;
		if (ml_phrase_append(phrase, element) < 0)
			YYNOMEM;
	}
	;

syllable:
	TOK_SYLLABLE melisma_sequence
	{
		$$.text = $1;
		$$.continuations = $2;
	}
	;

melisma_sequence:
	%empty
	{
		$$ = 0;
	}
	| melisma_sequence TOK_MELISMA
	{
		$$ = $1 + 1;
	}
	;

%%

void
yyerror(YYLTYPE *loc, yyscan_t scanner, struct ml_phrase *phrase, const char *message)
{
	(void)scanner;
	(void)phrase;

	fprintf(stderr, "%u:%u: %s\n",
		loc->first_line,
		loc->first_column,
		message);
}

