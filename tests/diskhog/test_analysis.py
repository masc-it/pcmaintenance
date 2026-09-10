from pc_maintenance.diskhog.analysis import analyze
from pc_maintenance.diskhog.scanner import Node

MIB = 1024 * 1024


def add(parent, name, size=0, *, is_dir=False, opaque=False):
    node = Node(name, parent=parent, size=size, is_dir=is_dir, opaque=opaque)
    parent.children.append(node)
    return node


def test_named_directories_and_models_are_classified():
    root = Node("/scan", is_dir=True)
    modules = add(root, "node_modules", 10 * MIB, is_dir=True, opaque=True)
    data = add(root, "data", 20 * MIB, is_dir=True)
    model = add(root, "model.gguf", 60 * MIB)
    add(root, ".venv", 512, is_dir=True, opaque=True)

    result = analyze(root)

    assert result.categories == {
        "Node modules": [modules],
        "Data dirs": [data],
        "ML models & datasets": [model],
    }
    assert result.top_files == [(60 * MIB, model)]


def test_huggingface_cache_is_not_also_a_generic_cache():
    root = Node("/scan", is_dir=True)
    cache = add(root, ".cache", is_dir=True)
    generic = add(cache, "pip", 2 * MIB, is_dir=True)
    hf = add(cache, "huggingface", 8 * MIB, is_dir=True)
    hub = add(hf, "hub", 8 * MIB, is_dir=True)
    model = add(hub, "models--example", 8 * MIB, is_dir=True)

    result = analyze(root)

    assert result.categories["System & app caches"] == [generic]
    assert result.categories["ML models & datasets"] == [model]


def test_largest_files_are_sorted_limited_and_handle_equal_sizes():
    root = Node("/scan", is_dir=True)
    add(root, "small", MIB)
    first = add(root, "first", 3 * MIB)
    second = add(root, "second", 3 * MIB)
    largest = add(root, "largest", 4 * MIB)

    result = analyze(root, top_n=3)

    assert [size for size, _ in result.top_files] == [4 * MIB, 3 * MIB, 3 * MIB]
    assert {node for _, node in result.top_files} == {first, second, largest}


def test_empty_tree():
    result = analyze(Node("/scan", is_dir=True))

    assert result.categories == {}
    assert result.top_files == []
