cmake_minimum_required(VERSION 3.16)
project(melisma C)
include(CTest)

find_package(BISON REQUIRED)
find_package(FLEX REQUIRED)

BISON_TARGET(MelismaParser src/melisma_parser.y ${CMAKE_CURRENT_BINARY_DIR}/melisma_parser.tab.c DEFINES_FILE ${CMAKE_CURRENT_BINARY_DIR}/melisma_parser.tab.h)

FLEX_TARGET(MelismaLexer src/melisma_lexer.l ${CMAKE_CURRENT_BINARY_DIR}/melisma_lexer.c DEFINES_FILE ${CMAKE_CURRENT_BINARY_DIR}/melisma_lexer.h)

ADD_FLEX_BISON_DEPENDENCY(MelismaLexer MelismaParser)

add_executable(melisma src/main.c src/melisma.c src/binder.c src/midi.c src/serializer.c ${BISON_MelismaParser_OUTPUTS} ${FLEX_MelismaLexer_OUTPUTS})

target_include_directories(melisma PRIVATE ${CMAKE_CURRENT_BINARY_DIR} ${CMAKE_CURRENT_SOURCE_DIR}/src)

if(BUILD_TESTING)
	add_executable(binder_test
		tests/binder_test.c
		src/binder.c
	)

	target_include_directories(binder_test PRIVATE
		${CMAKE_CURRENT_SOURCE_DIR}/src
	)

	add_test(NAME binder COMMAND binder_test)
	add_executable(note_sequence_test
		tests/note_sequence_test.c
		src/midi.c
	)

	target_include_directories(note_sequence_test PRIVATE
		${CMAKE_CURRENT_SOURCE_DIR}/src
	)

	add_test(NAME note_sequence COMMAND note_sequence_test)
	add_executable(cli_test
		tests/cli_test.c
	)

	add_dependencies(cli_test melisma)

	add_test(NAME cli
		COMMAND cli_test
			$<TARGET_FILE:melisma>
			${CMAKE_CURRENT_BINARY_DIR}
	)
	add_executable(serializer_test
		tests/serializer_test.c
		src/binder.c
		src/serializer.c
	)

	target_include_directories(serializer_test PRIVATE
		${CMAKE_CURRENT_SOURCE_DIR}/src
	)

	add_test(NAME serializer COMMAND serializer_test)
	add_executable(parser_test
		tests/parser_test.c
		src/melisma.c
		${BISON_MelismaParser_OUTPUTS}
		${FLEX_MelismaLexer_OUTPUTS}
	)

	target_include_directories(parser_test PRIVATE
		${CMAKE_CURRENT_BINARY_DIR}
		${CMAKE_CURRENT_SOURCE_DIR}/src
	)

	add_test(NAME parser COMMAND parser_test)
endif()
