#!/usr/bin/env python3
"""Compile the production import_commit body against a small buffer test double.

Usage: python3 shm-buffer-memoizer-regression.py path/to/src/steamcompmgr.cpp

This intentionally extracts the real production function after the patch stack has
been applied.  A hand-maintained copy of its cache condition would let the test
pass while import_commit regressed.
"""

import os
import pathlib
import subprocess
import sys
import tempfile


MEMOIZER = "static gamescope::CBufferMemoizer s_BufferMemos;"
NEXT_FUNCTION = "static int32_t\nwindow_last_done_commit_index"


def extract_import_commit(source: str) -> str:
    """Return the memoizer and complete import_commit definition from production."""
    try:
        start = source.index(MEMOIZER)
        end = source.index(NEXT_FUNCTION, start)
    except ValueError as error:
        raise RuntimeError("could not locate the bounded import_commit region") from error

    extracted = source[start:end]
    if "import_commit" not in extracted or "vulkan_create_texture_from_wlr_buffer" not in extracted:
        raise RuntimeError("bounded region does not contain the production import_commit body")
    return extracted


STUBS = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <map>
#include <memory>
#include <optional>
#include <utility>
#include <vector>

struct wlr_buffer;
struct CVulkanTexture
{
	int uploadedPixels = 0;
};

namespace gamescope
{
template <typename T>
class OwningRc;

template <typename T>
class Rc
{
public:
	Rc() = default;
	Rc( std::nullptr_t ) {}
	Rc( T *newValue ) : value( newValue ) {}

	T *operator->() const { return value; }
	operator T *() const { return value; }
	bool operator==( std::nullptr_t ) const { return value == nullptr; }

private:
	T *value = nullptr;
};

template <typename T>
class OwningRc
{
public:
	OwningRc() = default;
	OwningRc( std::nullptr_t ) {}
	OwningRc( T *newValue ) : value( newValue ) {}

	operator bool() const { return value != nullptr; }
	operator Rc<T>() const { return Rc<T>( value ); }
	bool operator==( std::nullptr_t ) const { return value == nullptr; }

private:
	T *value = nullptr;
};
struct IBackendFb {};

class CBufferMemoizer
{
public:
	OwningRc<CVulkanTexture> LookupVulkanTexture( wlr_buffer *buffer )
	{
		const auto found = textures.find( buffer );
		return found == textures.end() ? nullptr : found->second;
	}

	void MemoizeBuffer( wlr_buffer *buffer, OwningRc<CVulkanTexture> texture )
	{
		textures[buffer] = texture;
	}

	void Clear()
	{
		textures.clear();
	}

	std::size_t Size() const
	{
		return textures.size();
	}

private:
	std::map<wlr_buffer *, OwningRc<CVulkanTexture>> textures;
};
}

struct wlr_surface {};
struct wl_resource {};
struct wlserver_vk_swapchain_feedback {};
struct wlr_dmabuf_attributes { int unused = 0; };

struct wlr_buffer
{
	bool isDmabuf = false;
	bool failUpload = false;
	int pixels = 0;
	int dmabufQueries = 0;
};

struct steamcompmgr_win_t
{
	uint64_t seq = 17;
	uint32_t appID = 42;
};

struct commit_t
{
	uint64_t win_seq = 0;
	wlr_surface *surf = nullptr;
	wlr_buffer *buf = nullptr;
	bool async = false;
	bool fifo = false;
	bool is_steam = false;
	uint32_t appID = 0;
	std::vector<wl_resource *> presentation_feedbacks;
	wlserver_vk_swapchain_feedback feedback;
	std::optional<uint32_t> present_id;
	uint64_t desired_present_time = 0;
	gamescope::Rc<CVulkanTexture> vulkanTex = nullptr;
};

static bool window_is_steam( steamcompmgr_win_t * ) { return false; }
static bool window_is_vr_scene_app( steamcompmgr_win_t * ) { return false; }
static bool window_is_vr_app( steamcompmgr_win_t * ) { return false; }

static bool wlr_buffer_get_dmabuf( wlr_buffer *buffer, wlr_dmabuf_attributes * )
{
	buffer->dmabufQueries++;
	return buffer->isDmabuf;
}

struct FakeBackend
{
	gamescope::IBackendFb backendFb;
	int imports = 0;

	gamescope::OwningRc<gamescope::IBackendFb> ImportDmabufToBackend( wlr_dmabuf_attributes * )
	{
		imports++;
		return &backendFb;
	}
};

static FakeBackend g_backend;
static int g_uploads = 0;

static FakeBackend *GetBackend()
{
	return &g_backend;
}

static gamescope::OwningRc<CVulkanTexture>
vulkan_create_texture_from_wlr_buffer( wlr_buffer *buffer,
	gamescope::OwningRc<gamescope::IBackendFb> )
{
	g_uploads++;
	if ( buffer->failUpload )
		return nullptr;
	return new CVulkanTexture{ buffer->pixels };
}
'''

TESTS = r'''
static gamescope::Rc<commit_t> Import( steamcompmgr_win_t &window, wlr_buffer &buffer )
{
	return import_commit( &window, nullptr, &buffer, false, {}, {}, std::nullopt, 0, false );
}

static void Reset()
{
	s_BufferMemos.Clear();
	g_backend.imports = 0;
	g_uploads = 0;
}

static void TestReusedShmBuffersRefreshEveryCommit()
{
	Reset();
	steamcompmgr_win_t window;
	wlr_buffer pool[] = {
		{ false, false, 10 },
		{ false, false, 20 },
		{ false, false, 30 },
	};
	CVulkanTexture *firstSnapshots[3] = {};

	for ( int i = 0; i < 3; ++i )
	{
		commit_t *commit = Import( window, pool[i] );
		assert( commit != nullptr );
		firstSnapshots[i] = commit->vulkanTex;
		assert( firstSnapshots[i]->uploadedPixels == ( i + 1 ) * 10 );
	}

	for ( int i = 0; i < 3; ++i )
		pool[i].pixels += 100;

	for ( int i = 0; i < 3; ++i )
	{
		commit_t *commit = Import( window, pool[i] );
		assert( commit != nullptr );
		assert( commit->vulkanTex != firstSnapshots[i] );
		assert( commit->vulkanTex->uploadedPixels == ( i + 1 ) * 10 + 100 );
		assert( firstSnapshots[i]->uploadedPixels == ( i + 1 ) * 10 );
		assert( pool[i].dmabufQueries == 2 );
	}

	assert( g_uploads == 6 );
	assert( g_backend.imports == 0 );
	assert( s_BufferMemos.Size() == 0 );
}

static void TestDmaBufRetainsOneImportedTexture()
{
	Reset();
	steamcompmgr_win_t window;
	wlr_buffer buffer{ true, false, 7 };
	commit_t *first = Import( window, buffer );
	assert( first != nullptr );
	buffer.pixels = 99;
	commit_t *second = Import( window, buffer );
	assert( second != nullptr );
	assert( first->vulkanTex == second->vulkanTex );
	assert( first->vulkanTex->uploadedPixels == 7 );
	assert( g_uploads == 1 );
	assert( g_backend.imports == 1 );
	assert( s_BufferMemos.Size() == 1 );
	assert( buffer.dmabufQueries == 2 );
}

static void TestFailedImportIsNotMemoized()
{
	Reset();
	steamcompmgr_win_t window;
	wlr_buffer buffer{ false, true, 55 };
	assert( Import( window, buffer ) == nullptr );
	assert( g_uploads == 1 );
	assert( s_BufferMemos.Size() == 0 );
	buffer.failUpload = false;
	commit_t *commit = Import( window, buffer );
	assert( commit != nullptr );
	assert( commit->vulkanTex->uploadedPixels == 55 );
	assert( g_uploads == 2 );
	assert( s_BufferMemos.Size() == 0 );
}

int main()
{
	TestReusedShmBuffersRefreshEveryCommit();
	TestDmaBufRetainsOneImportedTexture();
	TestFailedImportIsNotMemoized();
	std::puts( "Gamescope SHM buffer memoizer regression test passed" );
}
'''


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {pathlib.Path(sys.argv[0]).name} src/steamcompmgr.cpp")

    source_path = pathlib.Path(sys.argv[1])
    production = extract_import_commit(source_path.read_text())
    temp_parent = os.environ.get("TMPDIR")
    with tempfile.TemporaryDirectory(prefix="gamescope-shm-memo-", dir=temp_parent) as temporary:
        cpp = pathlib.Path(temporary) / "shm-buffer-memoizer-regression.cpp"
        binary = pathlib.Path(temporary) / "shm-buffer-memoizer-regression"
        cpp.write_text(STUBS + "\n" + production + "\n" + TESTS)
        compiler = os.environ.get("CXX", "g++")
        subprocess.run(
            [compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-UNDEBUG", str(cpp), "-o", str(binary)],
            check=True,
        )
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
