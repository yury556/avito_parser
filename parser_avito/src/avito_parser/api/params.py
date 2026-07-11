

def build_api_params(search_core: dict) -> dict:
    params = {}

    base_keys = [
        'categoryId',
        'locationId',
        'verticalCategoryId',
        'rootCategoryId',
        'localPriority',
        'geoCoords',
    ]

    for key in base_keys:
        value = search_core.get(key)
        if value not in (None, [], ''):
            params[key] = str(value)

    # цена
    if search_core.get('priceMax'):
        params['pmax'] = search_core['priceMax']

    if search_core.get('priceMin'):
        params['pmin'] = search_core['priceMin']

    # продавец
    if search_core.get('owner'):
        params['user'] = search_core['owner']

    # доставка
    if search_core.get('withDeliveryOnly'):
        params['cd'] = 1

    # радиус
    if search_core.get('searchRadius'):
        params['radius'] = search_core['searchRadius']

    # сложные фильтры
    if search_core.get('params'):
        params.update(normalize_params(search_core['params']))

    return params

def normalize_params(params_dict: dict) -> dict:
    result = {}

    for key, values in params_dict.items():
        if not values:
            continue

        # если список
        if isinstance(values, list):
            for i, v in enumerate(values):
                # чаще всего работает без индекса
                result[f'params[{key}]'] = str(v)

        else:
            result[f'params[{key}]'] = str(values)

    return result