from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    if isinstance(dictionary, dict):
        return dictionary.get(key)
    return None

@register.filter
def to_range(start, end):
    return range(start, end)

@register.filter
def split(value, delimiter):
    return value.split(delimiter)

@register.filter
def to_int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0