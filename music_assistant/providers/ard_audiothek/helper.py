"""Helper to provide the GraphQL Queries."""

from gql import gql

image_list = """
imagesList {
  title
  url
  width
  aspectRatio
}
"""

audio_list = """
audioList {
  audioBitrate
  href
  audioCodec
  availableFrom
  availableTo
}
"""

organizations_query = gql(
    """
query Organizations {
  organizations {
    nodes {
      coreId
      name
      title
      publicationServicesByOrganizationName {
        nodes {
          coreId
          title"""
    + image_list
    + """
        }
      }
    }
  }
}
"""
)

publications_query = gql(
    """
query PublicationServices ($coreId: String!) {
  organizationByCoreId(coreId: $coreId) {
    publicationServicesByOrganizationName {
      nodes {
          coreId
          title
          synopsis"""
    + image_list
    + """
      }
    }
  }
}
"""
)

publications_list_query = gql(
    """
query Publications($coreId: String!) {
  publicationServiceByCoreId(coreId: $coreId) {
    title
    permanentLivestreams {
      nodes {
        current
        title"""
    + audio_list
    + """
        coreId
        summary"""
    + image_list
    + """
      }
    }
    genre
    synopsis
    socialMediaAccounts {
      url
      service
    }
    shows {
      nodes {
        coreId
        title
        synopsis"""
    + image_list
    + """
        editorialCategoriesList {
          title
        }
      }
    }
  }
}
"""
)


radio_metadata_query = gql(
    """
query PublicationServiceMetadata($coreId: String!) {
  publicationServiceByCoreId(coreId: $coreId) {
    genre"""
    + image_list
    + """
    socialMediaAccounts {
      url
      service
    }
    homepageUrl
    synopsis
  }
}
"""
)

livestream_query = gql(
    """
query Livestream($coreId: String!) {
  permanentLivestreamByCoreId(coreId: $coreId) {
    publisherCoreId
    summary
    current
    title"""
    + image_list
    + audio_list
    + """
  }
}
"""
)

show_length_query = gql("""
query Show($showId: ID!) {
  show(id: $showId) {
    items {
      totalCount
    }
  }
}
""")

show_query = gql(
    """
query Show($showId: ID!, $first: Int, $offset: Int) {
  show(id: $showId) {
    synopsis
    title
"""
    + image_list
    + """
    publicationService {
      title
    }
    items(first: $first, offset: $offset) {
      totalCount
      nodes {
        duration"""
    + audio_list
    + """
        title
        titleClean
        titleWithoutNumber
        episodeNumber"""
    + image_list
    + """
        coreId
        summary
      }
    }
    showType
    editorialCategoriesList {
      title
    }
  }
}
"""
)

show_episode_query = gql(
    """
query ShowEpisode($coreId: String!) {
  itemByCoreId(coreId: $coreId) {
      duration"""
    + audio_list
    + """
      title
      titleClean
      titleWithoutNumber
      episodeNumber"""
    + image_list
    + """
      coreId
    showId
    rowId
    synopsis
    summary
  }
}
"""
)


ard_search_query = gql(
    """
query Search($query: String, $limit: Int) {
  search(query: $query, limit: $limit) {
    shows {
      totalCount
      title
      nodes {
        synopsis
        title
        coreId"""
    + image_list
    + """
        publicationService {
          title
        }
        items {
          totalCount
        }
        showType
        editorialCategoriesList {
          title
        }
      }
    }
  }
}
"""
)
