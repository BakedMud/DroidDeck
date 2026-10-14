"""Exercise the patched upstream converter; HYBRIS_CLIP_SOURCE enables the CI gate."""

import ctypes
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

if __package__:
    from .test_hybris_vendor_compat import vulkan_include
else:
    from test_hybris_vendor_compat import vulkan_include


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "tools/hybris/patches/0006-clip-shader-layout.patch"
WORD = ctypes.c_uint32
WORD_POINTER = ctypes.POINTER(WORD)


def instructions(data):
    words = struct.unpack(f"<{len(data) // 4}I", data)
    cursor = 5
    while cursor < len(words):
        count = words[cursor] >> 16
        assert count and cursor + count <= len(words)
        yield words[cursor] & 0xffff, words[cursor + 1:cursor + count]
        cursor += count


def shader(stage, whole_array=False):
    vertex = stage == "vertex"
    return "\n".join([
        "OpCapability Shader",
        "OpCapability ClipDistance" if vertex else "",
        # A recognized extension keeps the upstream cleanup conservative. The
        # direct-array conversion must be valid without relying on that cleanup.
        'OpExtension "SPV_KHR_storage_buffer_storage_class"',
        "OpMemoryModel Logical GLSL450",
        ('OpEntryPoint Vertex %main "main" %clip %position' if vertex else
         'OpEntryPoint Fragment %main "main" %color'),
        "" if vertex else "OpExecutionMode %main OriginUpperLeft",
        "OpSource Unknown 0",
        'OpSourceContinued "regression fixture"',
        'OpName %main "main"',
        'OpModuleProcessed "clip shader regression"',
        ("OpDecorate %clip BuiltIn ClipDistance\nOpDecorate %position BuiltIn Position"
         if vertex else "OpDecorate %color Location 0"),
        "%void = OpTypeVoid",
        "%function = OpTypeFunction %void",
        "%float = OpTypeFloat 32",
        "%uint = OpTypeInt 32 0",
        "%zero_index = OpConstant %uint 0",
        "%one_index = OpConstant %uint 1",
        "%two = OpConstant %uint 2",
        "%one = OpConstant %float 1",
        "%vector = OpTypeVector %float 4",
        "%rgba = OpConstantComposite %vector %one %one %one %one",
        "%vector_pointer = OpTypePointer Output %vector",
        ("%array = OpTypeArray %float %two\n"
         "%array_pointer = OpTypePointer Output %array\n"
         "%float_pointer = OpTypePointer Output %float\n"
         "%values = OpConstantComposite %array %one %one\n"
         "%clip = OpVariable %array_pointer Output\n"
         "%position = OpVariable %vector_pointer Output" if vertex else
         "%color = OpVariable %vector_pointer Output"),
        "%main = OpFunction %void None %function",
        "%entry = OpLabel",
        (("OpStore %clip %values" if whole_array else
          "%first = OpAccessChain %float_pointer %clip %zero_index\n"
          "OpStore %first %one\n"
          "%second = OpAccessChain %float_pointer %clip %one_index\n"
          "OpStore %second %one") + "\nOpStore %position %rgba" if vertex else
         "OpStore %color %rgba"),
        "OpReturn",
        "OpFunctionEnd",
        "",
    ])


class HybrisClipShaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source_value = os.environ.get("HYBRIS_CLIP_SOURCE")
        if not source_value:
            raise unittest.SkipTest("set HYBRIS_CLIP_SOURCE to the pinned libhybris checkout")
        # An explicitly enabled gate must fail if its tools/source are missing.
        cls.compiler = shutil.which(os.environ.get("CC", "cc"))
        cls.assembler = shutil.which("spirv-as")
        cls.validator = shutil.which("spirv-val")
        if not all((cls.compiler, cls.assembler, cls.validator)):
            raise RuntimeError("HYBRIS_CLIP_SOURCE requires a C compiler and spirv-tools")
        source = Path(source_value) / "hybris/vulkan/compat"
        if not (source / "spirv_clip.c").is_file():
            raise RuntimeError("HYBRIS_CLIP_SOURCE must point to the pinned libhybris checkout")
        cls.temporary = tempfile.TemporaryDirectory(prefix="droiddeck-clip-shader-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        compat = cls.directory / "hybris/vulkan/compat"
        compat.mkdir(parents=True)
        shutil.copyfile(source / "spirv_clip.c", compat / "spirv_clip.c")
        apply = ["git", "-C", str(cls.directory), "apply"]
        check = subprocess.run(apply + ["--check", str(PATCH)], capture_output=True, text=True)
        if check.returncode == 0:
            subprocess.run(apply + [str(PATCH)], check=True, capture_output=True, text=True)
        else:
            # Also accept the full build checkout after the patch stack applied.
            subprocess.run(apply + ["--reverse", "--check", str(PATCH)],
                           check=True, capture_output=True, text=True)
        allocator = cls.directory / "allocator.c"
        allocator.write_text(
            "#include <vulkan/vulkan.h>\n#include <stdlib.h>\n"
            "void *hybris_scaled_alloc(const VkAllocationCallbacks *a, size_t n, "
            "VkSystemAllocationScope s) { (void)a; (void)s; return malloc(n); }\n"
            "void hybris_scaled_free(const VkAllocationCallbacks *a, void *p) "
            "{ (void)a; free(p); }\n")
        library = cls.directory / "clip.so"
        subprocess.run([
            cls.compiler, "-shared", "-fPIC", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
            "-I", str(vulkan_include()), "-I", str(source), str(compat / "spirv_clip.c"),
            *(str(source / name) for name in
              ("spirv_builtins.c", "spirv_entry.c", "spirv_decorations.c")),
            str(allocator), "-o", str(library)], check=True, capture_output=True, text=True)
        cls.library = ctypes.CDLL(str(library))
        arguments = [WORD_POINTER, ctypes.c_size_t, WORD, ctypes.c_void_p,
                     ctypes.POINTER(WORD_POINTER), ctypes.POINTER(ctypes.c_size_t),
                     ctypes.POINTER(ctypes.c_uint)]
        cls.library.hybris_spirv_clip_vertex.argtypes = arguments
        cls.library.hybris_spirv_clip_fragment.argtypes = arguments[:3] + [WORD] + arguments[3:]
        cls.library.hybris_scaled_free.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    def assemble(self, stage, whole_array=False):
        text = self.directory / "input.spvasm"
        binary = self.directory / "input.spv"
        text.write_text(shader(stage, whole_array))
        subprocess.run([self.assembler, "--target-env", "vulkan1.1", str(text), "-o", str(binary)],
                       check=True, capture_output=True, text=True)
        data = binary.read_bytes()
        self.validate(data)
        return data

    def validate(self, data):
        binary = self.directory / "validate.spv"
        binary.write_bytes(data)
        result = subprocess.run([self.validator, "--target-env", "vulkan1.1", str(binary)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def convert(self, data, stage):
        code = (WORD * (len(data) // 4)).from_buffer_copy(data)
        output, size, changed = WORD_POINTER(), ctypes.c_size_t(), ctypes.c_uint()
        arguments = [code, len(data), 7]
        if stage == "fragment":
            arguments.append(2)
        arguments += [None, ctypes.byref(output), ctypes.byref(size), ctypes.byref(changed)]
        function = (self.library.hybris_spirv_clip_vertex if stage == "vertex" else
                    self.library.hybris_spirv_clip_fragment)
        result = function(*arguments)
        try:
            self.assertEqual(result, 0)
            self.assertEqual(changed.value, 1)
            return ctypes.string_at(output, size.value)
        finally:
            self.library.hybris_scaled_free(None, output)

    def test_direct_array_keeps_interface_and_stores(self):
        for whole_array in (False, True):
            with self.subTest(whole_array=whole_array):
                original = self.assemble("vertex", whole_array)
                converted = self.convert(original, "vertex")
                self.validate(converted)
                before, after = list(instructions(original)), list(instructions(converted))
                self.assertEqual([v for op, v in before if op == 15],
                                 [v for op, v in after if op == 15])
                self.assertEqual([v for op, v in before if op in (59, 62, 65, 66)],
                                 [v for op, v in after if op in (59, 62, 65, 66)])
                self.assertFalse(any(op == 71 and v[1:] == (11, 3) for op, v in after))
                self.assertTrue(any(op == 71 and v[1:] == (30, 7) for op, v in after))

    def test_fragment_output_validates(self):
        self.validate(self.convert(self.assemble("fragment"), "fragment"))

    def test_execution_mode_id_stays_before_annotations(self):
        # Ubuntu's older SPIR-V tools do not know FPFastMathDefault. Exercise the
        # real converter's opcode331 layout handling independently of validator
        # support; current runtime dumps additionally validate with recent tools.
        for stage in ("vertex", "fragment"):
            with self.subTest(stage=stage):
                original = self.assemble(stage)
                stream = list(instructions(original))
                main = next(v[1] for op, v in stream if op == 15)
                float_type = next(v[0] for op, v in stream if op == 22)
                zero = next(v[1] for op, v in stream if op == 43 and v[2] == 0)
                position = max(i for i, (op, _) in enumerate(stream) if op in (15, 16)) + 1
                mode = (main, 6028, float_type, zero)  # FPFastMathDefault
                stream.insert(position, (331, mode))
                words = list(struct.unpack("<5I", original[:20]))
                for opcode, operands in stream:
                    words.extend([((len(operands) + 1) << 16) | opcode, *operands])
                changed = self.convert(struct.pack(f"<{len(words)}I", *words), stage)
                emitted = list(instructions(changed))
                self.assertEqual([v for op, v in emitted if op == 331], [mode])
                mode_index = next(i for i, (op, _) in enumerate(emitted) if op == 331)
                annotation_index = next(i for i, (op, _) in enumerate(emitted) if op in (71, 72))
                self.assertLess(mode_index, annotation_index)


if __name__ == "__main__":
    unittest.main()
