package com.droiddeck.launcher.session

import com.droiddeck.launcher.core.LogRedactor
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.zip.ZipFile

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class SessionLogShareTest {
    @get:Rule val tmp = TemporaryFolder()

    private val secret = "GET /x?sessionid=abcdef123456 200"
    private val leaked = "abcdef123456"

    private fun zip(folder: File): Map<String, String> =
        ZipFile(SessionLogShare.zipFolder(RuntimeEnvironment.getApplication(), folder)!!).use { z ->
            z.entries().toList().associate { it.name.substringAfter('/') to z.getInputStream(it).bufferedReader().readText() }
        }

    private fun record(folder: File, vararg files: String, rules: Int = LogRedactor.RULES_VERSION) {
        File(folder, SessionArtifacts.SCRUBBED_TREE_MARKER).writeText(
            "rules $rules\n" + files.joinToString("") { val f = File(folder, it); "${f.length()}\t${f.lastModified()}\t$it\n" })
    }

    @Test fun aRecordedUnchangedFileIsScrubbedAgainOnTheWayIntoTheZip() {
        val folder = tmp.newFolder("2026-10-06-01-steam")
        File(folder, "session.log").writeText("$secret\n")
        record(folder, "session.log")
        assertFalse(zip(folder).getValue("session.log").contains(leaked))
    }

    @Test fun anythingTheRecordDoesNotVouchForIsScrubbed() {
        val folder = tmp.newFolder("2026-10-06-02-steam")
        File(folder, "app.log").writeText("ok\n")
        File(folder, "session.log").writeText("ok\n")
        File(folder, "steam/deep").mkdirs()
        File(folder, "steam/deep/nested.txt").writeText("$secret\n")
        File(folder, ".hidden.txt").writeText("$secret\n")
        record(folder, "app.log", "session.log")
        File(folder, "app.log").appendText("$secret\n")
        File(folder, "session.log").apply { writeText("$secret\n".padEnd(3, ' ')); setLastModified(1_000L) }
        File(folder, "events.jsonl").writeText("$secret\n")

        val out = zip(folder)
        for (name in listOf("app.log", "session.log", "steam/deep/nested.txt", ".hidden.txt", "events.jsonl"))
            assertFalse(name, out.getValue(name).contains(leaked))
    }

    @Test fun aRecordUnderOtherRedactorRulesVouchesForNothing() {
        val folder = tmp.newFolder("2026-10-06-03-steam")
        File(folder, "session.log").writeText("$secret\n")
        record(folder, "session.log", rules = LogRedactor.RULES_VERSION - 1)
        assertFalse(zip(folder).getValue("session.log").contains(leaked))
    }

    @Test fun olderRuleRecordReScrubsDeviceIdentifiers() {
        val folder = tmp.newFolder("2026-10-07-01-steam")
        File(folder, "network.txt").writeText("mac 02:ab:cd:ef:12:34\n")
        File(folder, "steam").mkdirs()
        File(folder, "steam/controller_support.txt").writeText("Serial number: controller-123456\n")
        record(folder, "network.txt", "steam/controller_support.txt", rules = 1)

        val out = zip(folder)
        assertEquals("mac <redacted:mac>\n", out.getValue("network.txt"))
        assertEquals("Serial number: <redacted:serial>\n", out.getValue("steam/controller_support.txt"))
    }

    @Test fun withoutARecordEveryFileIsScrubbed() {
        val folder = tmp.newFolder("2026-10-06-04-steam")
        File(folder, "session.log").writeText("$secret\n")
        File(folder, "steam").mkdirs()
        File(folder, "steam/console_log.txt").writeText("$secret\n")
        val out = zip(folder)
        assertFalse(out.getValue("session.log").contains(leaked))
        assertFalse(out.getValue("steam/console_log.txt").contains(leaked))
    }

    @Test fun theEndingScrubsEveryFolderAndRecordsWhatItScrubbed() {
        val folder = tmp.newFolder("2026-10-06-05-steam")
        File(folder, "session.log").writeText("$secret\n")
        File(folder, "droiddeck-esync").mkdirs()
        File(folder, "droiddeck-esync/launches.log").writeText("$secret\n")
        File(folder, "steam/deep").mkdirs()
        File(folder, "steam/deep/nested.txt").writeText("$secret\n")

        SessionArtifacts.scrubAndMark(folder)

        for (name in listOf("session.log", "droiddeck-esync/launches.log", "steam/deep/nested.txt"))
            assertFalse(name, File(folder, name).readText().contains(leaked))
        assertEquals(setOf("session.log", "droiddeck-esync/launches.log", "steam/deep/nested.txt"),
            SessionArtifacts.scrubbedFiles(folder))
        assertTrue(zip(folder).values.none { it.contains(leaked) })
    }

    @Test fun aFileThatCouldNotBeScrubbedLeavesTheFolderUnrecorded() {
        val folder = tmp.newFolder("2026-10-06-06-steam")
        File(folder, "session.log").writeText("$secret\n")
        org.junit.Assume.assumeFalse(System.getProperty("os.name").startsWith("Windows"))
        folder.setWritable(false)
        try {
            SessionArtifacts.scrubAndMark(folder)
        } finally {
            folder.setWritable(true)
        }
        assertTrue(File(folder, "session.log").readText().contains(leaked))
        assertFalse(File(folder, SessionArtifacts.SCRUBBED_TREE_MARKER).exists())
        assertTrue(SessionArtifacts.scrubbedFiles(folder).isEmpty())
        assertTrue(zip(folder).values.none { it.contains(leaked) })
    }

    @Test fun steamLogsPastTheSizeLimitStayOut() {
        val folder = tmp.newFolder("2026-10-06-07-steam")
        File(folder, "session.log").writeText("ok\n")
        File(folder, "steam").mkdirs()
        File(folder, "steam/console_log.txt").writeText("ok\n")
        File(folder, "steam/cef_log.previous.txt").outputStream().use { out ->
            val line = ByteArray(1024) { 'x'.code.toByte() }.also { it[it.size - 1] = '\n'.code.toByte() }
            repeat(9 * 1024) { out.write(line) }
        }
        val names = zip(folder).keys
        assertTrue("steam/console_log.txt" in names)
        assertFalse("steam/cef_log.previous.txt" in names)
    }

    @Test fun streamingComparisonPreservesUnchangedFileAndReplacesChangedFile() {
        val folder = tmp.newFolder("2026-10-06-08-steam")
        val unchanged = File(folder, "unchanged.log")
        unchanged.bufferedWriter().use { out -> repeat(5000) { out.write("safe diagnostic $it\n") } }
        val originalModified = 1_600_000_000_000L
        assertTrue(unchanged.setLastModified(originalModified))
        val changed = File(folder, "changed.log")
        changed.bufferedWriter().use { out ->
            repeat(5000) { out.write("safe diagnostic $it\n") }
            out.write("$secret\n")
        }

        assertTrue(SessionArtifacts.scrubAndMark(folder))

        assertEquals(originalModified, unchanged.lastModified())
        assertFalse(changed.readText().contains(leaked))
        assertTrue(changed.readText().contains("sessionid=<redacted:token>"))
    }

    @Test fun historicalScrubDefersWithoutLeavingMarkersAndResumes() {
        val folder = tmp.newFolder("2026-10-06-09-steam")
        val log = File(folder, "session.log").apply { writeText("$secret\n") }
        val rulesMarker = File(folder, ".scrubbed-r${LogRedactor.RULES_VERSION}").apply { writeText("stale\n") }
        val treeMarker = File(folder, SessionArtifacts.SCRUBBED_TREE_MARKER).apply { writeText("rules ${LogRedactor.RULES_VERSION}\n") }

        SessionState.running = true
        try {
            assertFalse(SessionArtifacts.scrubAndMark(folder, stopWhenSessionStarts = true))
        } finally {
            SessionState.running = false
        }
        assertFalse(rulesMarker.exists())
        assertFalse(treeMarker.exists())
        assertTrue(log.readText().contains(leaked))

        assertTrue(SessionArtifacts.scrubAndMark(folder, stopWhenSessionStarts = true))
        assertFalse(log.readText().contains(leaked))
        assertTrue(File(folder, ".scrubbed-r${LogRedactor.RULES_VERSION}").isFile)
    }

    @Test fun historicalScrubCancellationDuringStreamingPreservesTheOriginal() {
        val folder = tmp.newFolder("2026-10-06-10-steam")
        val original = "first safe line\n" + "x".repeat(20_000) + " $secret\n"
        val log = File(folder, "session.log").apply { writeText(original) }
        var checks = 0

        assertFalse(SessionArtifacts.scrubAndMark(folder, stopWhenSessionStarts = true) { ++checks >= 4 })

        assertEquals(original, log.readText())
        assertFalse(File(folder, ".session.log.scrub").exists())
        assertFalse(File(folder, SessionArtifacts.SCRUBBED_TREE_MARKER).exists())
    }

    @Test fun historicalScrubCancellationBeforeComparisonPreservesTheOriginal() {
        val folder = tmp.newFolder("2026-10-06-11-steam")
        val original = "safe diagnostic\n"
        val log = File(folder, "session.log").apply { writeText(original) }
        val temporary = File(folder, ".session.log.scrub")

        assertFalse(SessionArtifacts.scrubAndMark(folder, stopWhenSessionStarts = true) { temporary.length() > 0 })

        assertEquals(original, log.readText())
        assertFalse(temporary.exists())
        assertFalse(File(folder, SessionArtifacts.SCRUBBED_TREE_MARKER).exists())
    }

    @Test fun comparisonCancellationWinsOverAnImmediateLengthMismatch() {
        val left = tmp.newFile("left.log").apply { writeText("short\n") }
        val right = tmp.newFile("right.log").apply { writeText("a different length\n") }

        assertNull(SessionArtifacts.sameContents(left, right) { true })
    }

    @Test fun recoveryRetryKeepsItsSteamLogsWhenALaterSessionLeftNoFolder() {
        val context: android.content.Context = RuntimeEnvironment.getApplication()
        val folder = File(com.droiddeck.launcher.runtime.LinuxRuntime.logDir(context), "2026-10-08-01-steam").apply { mkdirs() }
        val saved = File(folder, "steam/console_log.txt").apply {
            parentFile!!.mkdirs()
            writeText("session A\n")
        }
        File(folder, "ended-without-teardown.txt").writeText("recovery attempted before session B\n")
        val runtime = File(com.droiddeck.launcher.runtime.LinuxRuntime.rootDir(context), "root/.local/share/Steam/logs/console_log.txt").apply {
            parentFile!!.mkdirs()
            writeText("session B\n")
        }

        SessionArtifacts.finishAbandoned(context)

        assertEquals("session A\n", saved.readText())
        assertEquals("session B\n", runtime.readText())
        assertTrue(File(folder, "ended-without-teardown.txt").readText().contains("runtime capture did not finish"))
        assertTrue(File(folder, SessionArtifacts.COMPLETE_MARKER).isFile)
    }

    @Test fun abandonedRecoveryDoesNotCopyLogsFromANewerCompletedSession() {
        val context: android.content.Context = RuntimeEnvironment.getApplication()
        val logs = com.droiddeck.launcher.runtime.LinuxRuntime.logDir(context)
        val folder = File(logs, "2026-10-08-02-steam").apply { mkdirs() }
        val saved = File(folder, "steam/console_log.txt").apply {
            parentFile!!.mkdirs()
            writeText("session A\n")
        }
        File(logs, "2026-10-08-03-steam").apply { mkdirs() }
            .resolve(SessionArtifacts.COMPLETE_MARKER).writeText("finished session B\n")
        File(com.droiddeck.launcher.runtime.LinuxRuntime.rootDir(context), "root/.local/share/Steam/logs/console_log.txt").apply {
            parentFile!!.mkdirs()
            writeText("session B\n")
        }

        SessionArtifacts.finishAbandoned(context)

        assertEquals("session A\n", saved.readText())
        assertTrue(File(folder, SessionArtifacts.COMPLETE_MARKER).isFile)
    }

    @Test fun startupRecoveryRunsBeforeVacMarkerArmWhileOrdinaryCleanupDefers() {
        val context: android.content.Context = RuntimeEnvironment.getApplication()
        val root = com.droiddeck.launcher.runtime.LinuxRuntime.rootDir(context)
        val folder = File(com.droiddeck.launcher.runtime.LinuxRuntime.logDir(context), "2999-12-31-99-startup-recovery")
        val marker = File(root, "root/.droiddeck-vac-diagnostics")
        val launches = File(root, "root/.droiddeck-vac-launches")
        val runtimeSteamLog = File(SessionPrefs.steamRoot(context, "arm64"), "logs/startup-recovery-vac.log")
        val previousRunning = SessionState.running
        val previousPhase = SessionState.phase
        try {
            folder.deleteRecursively()
            folder.mkdirs()
            SessionArtifacts.recordClient(folder, "arm64")
            marker.apply { parentFile!!.mkdirs(); writeText("0\narm64\n") }
            launches.writeText("startup VAC evidence\n")
            runtimeSteamLog.apply { parentFile!!.mkdirs(); writeText("startup VAC evidence\n") }
            SessionState.running = true
            SessionState.phase = SessionPhase.STARTING_GUEST

            SessionArtifacts.finishAbandoned(context)

            assertFalse(File(folder, "ended-without-teardown.txt").exists())
            assertFalse(File(folder, SessionArtifacts.COMPLETE_MARKER).exists())

            SessionArtifacts.finishAbandoned(context, beforeGuestStart = true)

            assertEquals("startup VAC evidence\n", File(folder, "steam/startup-recovery-vac.log").readText())
            assertTrue(File(folder, "vac.txt").readText().contains("startup VAC evidence"))
            assertTrue(File(folder, SessionArtifacts.COMPLETE_MARKER).isFile)
        } finally {
            SessionState.running = previousRunning
            SessionState.phase = previousPhase
            folder.deleteRecursively()
            runtimeSteamLog.delete()
            marker.delete()
            launches.delete()
        }
    }

    @Test fun startupRecoveryExcludesCurrentFolderAndCompletesDeferredRuntimeCapture() {
        val context: android.content.Context = RuntimeEnvironment.getApplication()
        val root = com.droiddeck.launcher.runtime.LinuxRuntime.rootDir(context)
        val logs = com.droiddeck.launcher.runtime.LinuxRuntime.logDir(context)
        val day = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())
        val folder = File(logs, "$day-9000-deferred-recovery")
        val marker = File(root, "root/.droiddeck-vac-diagnostics")
        val launches = File(root, "root/.droiddeck-vac-launches")
        val runtimeSteamLog = File(SessionPrefs.steamRoot(context, "arm64"), "logs/deferred-recovery-vac.log")
        val previousRunning = SessionState.running
        val previousPhase = SessionState.phase
        val previousLogsEnabled = SessionPrefs.logsEnabled(context)
        var currentFolder: File? = null
        try {
            folder.deleteRecursively()
            folder.mkdirs()
            SessionArtifacts.recordClient(folder, "arm64")
            File(folder, "ended-without-teardown.txt").writeText("recovery began before session start\n")
            marker.apply { parentFile!!.mkdirs(); writeText("0\narm64\n") }
            launches.writeText("deferred VAC evidence\n")
            runtimeSteamLog.apply { parentFile!!.mkdirs(); writeText("deferred VAC evidence\n") }
            SessionState.running = true
            SessionState.phase = SessionPhase.STARTING_GUEST
            SessionPrefs.setLogsEnabled(context, true)
            currentFolder = SessionPaths.beginOrCurrent(context, "current-session")

            SessionArtifacts.finishAbandoned(context, beforeGuestStart = true)

            assertEquals("deferred VAC evidence\n", File(folder, "steam/deferred-recovery-vac.log").readText())
            assertTrue(File(folder, "vac.txt").readText().contains("deferred VAC evidence"))
            assertTrue(File(folder, ".recovery-runtime-captured").isFile)
            assertTrue(File(folder, SessionArtifacts.COMPLETE_MARKER).isFile)
            assertFalse(File(currentFolder, "ended-without-teardown.txt").exists())
            assertFalse(File(currentFolder, SessionArtifacts.COMPLETE_MARKER).exists())
        } finally {
            SessionState.running = previousRunning
            SessionState.phase = previousPhase
            SessionPaths.take()?.let { SessionPaths.release(context, it) }
            currentFolder?.deleteRecursively()
            SessionPrefs.setLogsEnabled(context, previousLogsEnabled)
            folder.deleteRecursively()
            runtimeSteamLog.delete()
            marker.delete()
            launches.delete()
        }
    }

    @Test fun failedPreGuestRuntimeCaptureWritesNeitherCaptureNorCompleteMarker() {
        val context: android.content.Context = RuntimeEnvironment.getApplication()
        val root = com.droiddeck.launcher.runtime.LinuxRuntime.rootDir(context)
        val folder = File(com.droiddeck.launcher.runtime.LinuxRuntime.logDir(context), "9999-12-31-01-vac-failure")
        val marker = File(root, "root/.droiddeck-vac-diagnostics")
        val launches = File(root, "root/.droiddeck-vac-launches")
        val runtimeSteamLog = File(SessionPrefs.steamRoot(context, "arm64"), "logs/expired-recovery.log")
        val previousRunning = SessionState.running
        try {
            folder.deleteRecursively()
            folder.mkdirs()
            SessionArtifacts.recordClient(folder, "arm64")
            File(folder, "ended-without-teardown.txt").writeText("recovery began before session start\n")
            File(folder, "vac.txt").mkdirs()
            marker.apply { parentFile!!.mkdirs(); writeText("0\narm64\n") }
            launches.writeText("VAC evidence that cannot be reported\n")
            runtimeSteamLog.apply { parentFile!!.mkdirs(); writeText("session A\n") }
            SessionState.running = true

            SessionArtifacts.finishAbandoned(context, beforeGuestStart = true)

            assertFalse(File(folder, ".recovery-runtime-captured").exists())
            assertTrue(File(folder, ".recovery-runtime-expired").isFile)
            assertFalse(File(folder, SessionArtifacts.COMPLETE_MARKER).exists())

            File(folder, "vac.txt").deleteRecursively()
            runtimeSteamLog.writeText("session B\n")
            SessionArtifacts.finishAbandoned(context, beforeGuestStart = true)

            assertEquals("session A\n", File(folder, "steam/expired-recovery.log").readText())
            assertTrue(File(folder, "ended-without-teardown.txt").readText().contains("runtime capture did not finish"))
            assertTrue(File(folder, SessionArtifacts.COMPLETE_MARKER).isFile)
        } finally {
            SessionState.running = previousRunning
            folder.deleteRecursively()
            runtimeSteamLog.delete()
            marker.delete()
            launches.delete()
        }
    }
}
