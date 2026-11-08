# Engineering notes

The problems that took real work. Numbers come from runs on one NVIDIA DGX Spark (GB10, 128 GB unified memory, aarch64).

## The AI translates; code is the judge

An LLM in a game with a currency is an obvious target: if a player can talk the model into a stronger monster, the economy means nothing. So the model is never allowed to produce a number.

- **Its output is constrained by a JSON schema** (`format` in Ollama's chat API), at temperature 0. Parts and elements are enums, so the model cannot invent a `laser_cannon`. There is no stats field to fill in.
- **Stats come from code.** The total depends only on level, apart from rounding each stat, which can leave the sum 1 either side. The parts only decide how that total is split, with the effect of each part damped so nothing gets extreme (`compute_stats` in `game/parts.py`).
- **Power claims have somewhere to go.** "Attack 9999" lands in `ignored_claims`, and the player is told it did not count. This is for the player, not a defense: in the injection check the model skipped it twice, once because the description told it to, and the stat total did not move either time.
- **Code checks the output anyway** (`clean_parts`, `clean_elements`, and a move with an unknown type, fit or element is refused before charging), so the rules still hold if a model ignores the schema.

`game/tools/injection_check.py` tests this against the real model. See the README for the results, including where it falls short: words can't raise the total, but describing a bulky body buys a split of points that wins far too often.

There is a smaller version of the same idea on the image side. The image prompt written by the model sometimes dropped a feature, and a monster lost its tail flame that way. Now code writes the parts list back into every image prompt (`_features` in `game/ai.py`), so the picture has to match the profile.

## A UV unwrap that hung forever on GB10

The image-to-3D workflow froze inside ComfyUI's `UnwrapMesh`. One CPU core sat at 100%, GPU use was 0% at 11 W, and `/interrupt` had no effect. Reducing to 60,000 faces changed nothing.

The log stopped at `unwrap: ortho checks: 0/826`, which pointed at the wrong place. That progress bar clears itself when it finishes, so it looked stuck after it was already done. A benchmark of the O(n²) check confirmed this: 0.37 s for 8,000 edges.

`py-spy dump --native` (it needs `docker exec --privileged` for ptrace) showed the real position: `torch.linalg.solve` on a batch of fp64 dense systems, waiting on a mutex in libcublas and libcuda.

A small control settled it. In the same container, a batch of 20 systems of size 256 solved instantly in numpy on the CPU, and did not return within 5 minutes through `torch.linalg.solve` on the GPU.

The fix is a custom node of about ten lines, `gpu/patches/gb10_patches.py`. It forces that one function onto its existing CPU branch, without touching ComfyUI itself, so updates still apply cleanly. With it, a game-quality model takes under 3 minutes, and the unwrap is about 5 seconds of that.

## PyTorch can be 5.6 times slower without saying so

On this chip an older PyTorch misidentifies the GPU, and nothing reports an error:

| Container | PyTorch | Capability it reports | fp16 matmul | Memory bandwidth |
|---|---|---|---|---|
| ComfyUI image | 2.9.1 | (12, 1), correct | 63.0 TFLOPS | 233 GB/s |
| NVIDIA NGC 25.01 | 2.6.0 | (9, 0), mistaken for Hopper | 11.2 TFLOPS | 229 GB/s |

The bandwidth column is the control. It is the same in both, which rules out the hardware and the measurement, and leaves the kernel selection. This matters for anyone judging the machine: a project that pins an old PyTorch looks like the hardware cannot run it.

So every dependency here installs with `-c constraints.txt`, a file on the GPU machine pinning the PyTorch packages the container shipped with, after a `--dry-run` to confirm PyTorch is not in the install list. One unpinned install had already started downloading a replacement before I stopped it.

## Auto-rigging without Blender

Battle moves play out by body part, so every generated monster needs a skeleton and skin weights. SkinTokens produces both in one model, and is pip-installable on aarch64, unlike UniRig, whose compiled dependencies have no ARM packages. Three things stood in the way:

- **No Blender for Linux ARM,** and the official flow reads and writes models through it. `gpu/rig/rig_glb.py` uses the repo's raw vertex-and-face entry point instead, and writes the glTF skin itself with pygltflib: joint nodes, inverse bind matrices, `JOINTS_0` and `WEIGHTS_0`, in the original vertex order.
- **No flash-attn on GB10.** Imports look for it, so `sdpa_flash_shim.py` provides a stand-in built on SDPA. It has to be present only while SkinTokens imports and be removed straight after, because transformers looks up the install record for that name and raises a `KeyError`.
- **Coordinates.** The model was trained on Z-up data from Blender, and GLB is Y-up. The conversion happens in both directions, and the model's internal scale and offset are recovered by matching vertices one to one (residual about 3e-8).

Skeletons are sampled, so quality varies by seed. One seed gave a fire lizard no tail, another put seven joints in its neck, and a third was right. The fix is to run several seeds and score each one by how well the bones cover the mesh, rather than retrying only on errors (`gpu/rig/rig_roles.py`). The same file works out which chain of bones is the head, tail, legs or wings, which is what lets a tail whip swing the tail. A rig takes about a minute.

## Slow AI calls and shared state

An AI call takes seconds. Holding the game lock through it would freeze every other player, so the calls run outside the lock. That opens a gap: the world can change while the model is thinking.

These all happened or were reachable:

- A player releases a monster while it is learning a move, and pays for a move nothing will ever use.
- Two simultaneous requests both pass the "fewer than 4 moves" check.
- Several hatches at once pass the roster limit.
- A player switches monster while a growth preview is running, and the preview lands on the wrong one.
- A second growth starts while the first picture is being drawn. Both take the same version number, and the second never gets a 3D model.

The pattern that fixed them: check under the lock, count the work in progress, call the AI outside the lock, then **take the lock again and check everything again** before writing anything. Money follows the same shape: Tokens are charged under the lock, and a failed picture or a vanished monster refunds them. Fast work stays under the lock from start to finish: a battle turn, so two requests on the last turn can't both collect the reward, and creating a player, so saves never overlap. `game/tests/test_game.py` reproduces each race with stubbed AI calls that sleep at the right moment, so they run in CI with no GPU.

## Recording a 3D game frame by frame

The demo video needed smooth 30 fps from a WebGL page in a headless browser, where a screenshot takes longer than a frame. `game/tools/video/rec-lib.mjs` replaces `requestAnimationFrame` and `performance.now` with a virtual clock, so the page only moves when the recorder tells it to: advance 1/30 s, take a screenshot, repeat. Animations come out exact no matter how slow the capture is. Unchanged frames are stored once with a longer duration in an ffmpeg concat list.
