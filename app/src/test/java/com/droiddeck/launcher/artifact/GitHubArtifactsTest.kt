package com.droiddeck.launcher.artifact

import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class GitHubArtifactsTest {
    private val app get() = RuntimeEnvironment.getApplication()
    private val key = "gpu/system-vulkan/mali"
    private val defaults = listOf("Example/Drivers")
    private val source get() = RepositorySources.list(app, key, defaults).single()
    private val productionConnection = GitHubArtifacts.openConnection
    private val sha = "a".repeat(64)

    @After fun restoreTransport() { GitHubArtifacts.openConnection = productionConnection }

    private class Response(url: URL, private val status: Int = 200, private val body: String = "",
        private val location: String? = null) : HttpURLConnection(url) {
        override fun connect() {}
        override fun disconnect() {}
        override fun usingProxy() = false
        override fun getResponseCode() = status
        override fun getHeaderField(name: String?) = if (name == "Location") location else null
        override fun getInputStream() = ByteArrayInputStream(body.toByteArray())
        override fun getContentLengthLong() = -1L
    }

    private fun release(digest: String? = "sha256:$sha", assetUrl: String = "https://github.com/Example/Drivers/releases/download/v1/bundle.zip"): JSONObject {
        val asset = JSONObject().put("id", 9).put("name", "bundle.zip").put("size", 12)
            .put("browser_download_url", assetUrl).put("digest", digest ?: JSONObject.NULL)
        return JSONObject().put("id", 5).put("tag_name", "v1").put("name", "Driver 1")
            .put("assets", JSONArray().put(asset))
    }

    private fun successfulTransport(release: JSONObject = release()) {
        GitHubArtifacts.openConnection = { url ->
            Response(url, body = if (url.path.endsWith("/releases")) JSONArray().put(release).toString()
                else """{"id":123,"full_name":"Example/Drivers"}""")
        }
    }

    @Test fun refreshBindsIdentityAndRetainsLastGoodCatalogAfterFailure() {
        successfulTransport()
        val results = GitHubArtifacts.refreshReleases(app, key, defaults, source)
        assertEquals(123L, source.repositoryId)
        assertEquals(sha, results.single().assets.single().sha256)
        GitHubArtifacts.openConnection = { throw IOException("offline") }
        assertTrue(runCatching { GitHubArtifacts.refreshReleases(app, key, defaults, source) }.isFailure)
        assertEquals(results, GitHubArtifacts.cachedReleases(app, key, defaults, source))
    }

    @Test fun hiddenSourceHasNoCachedResultsAndMakesNoHttpRequests() {
        successfulTransport()
        GitHubArtifacts.refreshReleases(app, key, defaults, source)
        val previous = source
        RepositorySources.save(app, key, defaults, listOf(previous.copy(hidden = true)))
        var called = false
        GitHubArtifacts.openConnection = { called = true; throw IOException("must not run") }
        assertTrue(GitHubArtifacts.cachedReleases(app, key, defaults, previous).isEmpty())
        assertTrue(runCatching { GitHubArtifacts.refreshReleases(app, key, defaults, previous) }.isFailure)
        assertFalse(called)
    }

    @Test fun hidingDuringFetchRejectsResultsAndLeavesCacheUntouched() {
        successfulTransport()
        val before = GitHubArtifacts.refreshReleases(app, key, defaults, source)
        GitHubArtifacts.openConnection = { url ->
            if (url.path.endsWith("/releases")) {
                RepositorySources.save(app, key, defaults, listOf(source.copy(hidden = true)))
                Response(url, body = "[]")
            } else Response(url, body = """{"id":123,"full_name":"Example/Drivers"}""")
        }
        assertTrue(runCatching { GitHubArtifacts.refreshReleases(app, key, defaults, source) }.isFailure)
        RepositorySources.save(app, key, defaults, listOf(source.copy(hidden = false)))
        assertEquals(before, GitHubArtifacts.cachedReleases(app, key, defaults, source))
    }

    @Test fun reassignedRepositoryNameFailsClosed() {
        successfulTransport()
        GitHubArtifacts.refreshReleases(app, key, defaults, source)
        GitHubArtifacts.openConnection = { url -> Response(url, body = """{"id":999,"full_name":"Example/Drivers"}""") }
        assertTrue(runCatching { GitHubArtifacts.refreshReleases(app, key, defaults, source) }.isFailure)
        assertEquals(123L, source.repositoryId)
    }

    @Test fun missingDigestIsNotDownloadableAndForeignAssetUrlsAreRejected() {
        successfulTransport(release(digest = null))
        assertTrue(GitHubArtifacts.refreshReleases(app, key, defaults, source).single().assets.isEmpty())
        successfulTransport(release(assetUrl = "https://github.com/evil/Drivers/releases/download/v1/bundle.zip"))
        assertTrue(runCatching { GitHubArtifacts.refreshReleases(app, key, defaults, source) }.isFailure)
    }

    @Test fun releaseQueriesUseTheBoundNumericIdentityAndRejectNameRedirects() {
        val requests = ArrayList<String>()
        GitHubArtifacts.openConnection = { url ->
            requests.add(url.path)
            if (url.path.endsWith("/releases")) Response(url, 302, location = "https://api.github.com/repos/example/drivers/releases")
            else Response(url, body = """{"id":123,"full_name":"Example/Drivers"}""")
        }
        assertTrue(runCatching { GitHubArtifacts.refreshReleases(app, key, defaults, source) }.isFailure)
        assertEquals(listOf("/repos/example/drivers", "/repositories/123/releases"), requests)
    }

    @Test fun pinnedReleaseUsesExactTagWithoutScanningRecentPages() {
        val requests = ArrayList<String>()
        GitHubArtifacts.openConnection = { url ->
            requests += url.path
            when (url.path) {
                "/repos/example/drivers" -> Response(url, body = """{"id":123,"full_name":"Example/Drivers"}""")
                "/repositories/123/releases/tags/v1" -> Response(url, body = release().toString())
                else -> throw IOException("Unexpected latest-release lookup")
            }
        }
        assertEquals("v1", GitHubArtifacts.release(app, key, defaults, source, "v1").tag)
        assertEquals(listOf("/repos/example/drivers", "/repositories/123/releases/tags/v1"), requests)
    }

    @Test fun pinnedReleaseRejectsDifferentTagAndHiddenSource() {
        GitHubArtifacts.openConnection = { url ->
            Response(url, body = if (url.path.contains("/releases/tags/")) release().toString()
                else """{"id":123,"full_name":"Example/Drivers"}""")
        }
        assertTrue(runCatching { GitHubArtifacts.release(app, key, defaults, source, "v2") }.isFailure)
        RepositorySources.save(app, key, defaults, listOf(source.copy(hidden = true)))
        GitHubArtifacts.openConnection = { throw AssertionError("Hidden source must not make a request") }
        assertTrue(runCatching { GitHubArtifacts.release(app, key, defaults, source, "v1") }.isFailure)
    }

    @Test fun redirectsCannotEscapeHttpsOrRepositoryAndHaveAHopLimit() {
        val initial = "https://api.github.com/repos/example/drivers"
        for (destination in listOf("http://api.github.com/repos/example/drivers", "https://evil.example/payload", "https://api.github.com/repos/evil/drivers")) {
            var calls = 0
            GitHubArtifacts.openConnection = { url -> calls++; Response(url, 302, location = destination) }
            assertTrue(runCatching { GitHubArtifacts.refreshReleases(app, key, defaults, source) }.isFailure)
            assertEquals(1, calls)
        }
        var calls = 0
        GitHubArtifacts.openConnection = { url -> calls++; Response(url, 302, location = initial) }
        assertTrue(runCatching { GitHubArtifacts.fetch(initial, 128) { it == URI(initial) } }.isFailure)
        assertEquals(4, calls)
    }

    @Test fun unknownLengthMetadataCannotExceedByteLimit() {
        GitHubArtifacts.openConnection = { url -> Response(url, body = "123456789") }
        assertTrue(runCatching { GitHubArtifacts.fetch("https://api.github.com/repos/example/drivers", 8) { true } }.isFailure)
    }

    @Test fun releaseMetadataMustMatchDigestAndSizeAfterCdnRedirect() {
        val data = "{}"
        val bound = RepositorySources.bind(app, key, defaults, source, 123, RepositorySources.generation(app, key, defaults))
        val asset = GitHubArtifacts.Asset(9, "manifest.json", GitHubArtifacts.releaseAssetUrl(bound.repository, "v1", "manifest.json"), 2,
            RepositorySources.sha256(data.toByteArray()))
        GitHubArtifacts.openConnection = { url ->
            if (url.host == "github.com") Response(url, 302, location = "https://release-assets.githubusercontent.com/asset?id=9")
            else Response(url, body = data)
        }
        assertEquals(data, GitHubArtifacts.readReleaseAsset(app, key, defaults, bound, "v1", asset).toString(Charsets.UTF_8))
        assertTrue(runCatching { GitHubArtifacts.readReleaseAsset(app, key, defaults, bound, "v1", asset.copy(sha256 = sha)) }.isFailure)
    }

    @Test fun failedStreamingDownloadNeverReplacesExistingBytesOrLeavesPartialFiles() {
        val bound = RepositorySources.bind(app, key, defaults, source, 123, RepositorySources.generation(app, key, defaults))
        val data = "verified archive"
        val asset = GitHubArtifacts.Asset(9, "bundle.zip", GitHubArtifacts.releaseAssetUrl(bound.repository, "v1", "bundle.zip"),
            data.length.toLong(), RepositorySources.sha256(data.toByteArray()))
        val folder = File(app.cacheDir, "payloads").apply { mkdirs() }
        val destination = File(folder, "archive.zip").apply { writeText("old") }
        GitHubArtifacts.openConnection = { url -> Response(url, body = "wrong archive") }
        assertTrue(runCatching { GitHubArtifacts.downloadReleaseAsset(app, key, defaults, bound, "v1", asset, destination, 100) }.isFailure)
        assertEquals("old", destination.readText())
        assertEquals(listOf("archive.zip"), folder.list()!!.toList())
        GitHubArtifacts.openConnection = { url -> Response(url, body = data) }
        GitHubArtifacts.downloadReleaseAsset(app, key, defaults, bound, "v1", asset, destination, 100)
        assertEquals(data, destination.readText())
        GitHubArtifacts.openConnection = { throw IOException("must reuse verified download") }
        assertEquals(destination, GitHubArtifacts.downloadReleaseAsset(app, key, defaults, bound, "v1", asset, destination, 100))
    }

    @Test fun hidingDuringStreamingDownloadCannotPublishBytes() {
        val bound = RepositorySources.bind(app, key, defaults, source, 123, RepositorySources.generation(app, key, defaults))
        val asset = GitHubArtifacts.Asset(9, "bundle.zip", GitHubArtifacts.releaseAssetUrl(bound.repository, "v1", "bundle.zip"), 2,
            RepositorySources.sha256("{}".toByteArray()))
        val destination = File(app.cacheDir, "hidden/archive.zip")
        GitHubArtifacts.openConnection = { url ->
            RepositorySources.save(app, key, defaults, listOf(bound.copy(hidden = true)))
            Response(url, body = "{}")
        }
        assertTrue(runCatching { GitHubArtifacts.downloadReleaseAsset(app, key, defaults, bound, "v1", asset, destination, 100) }.isFailure)
        assertFalse(destination.exists())
        assertTrue(destination.parentFile!!.list()!!.isEmpty())
    }
}
